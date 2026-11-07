"""Servidor IRC local que representa uma sala do chat do site."""

import argparse
import asyncio
import contextlib
import datetime
import hashlib
import logging
import re
from pathlib import Path

from message_formatting import (
    CONTROL,
    clean_irc,
    extract_reply,
    html_to_irc,
    irc_to_bbcode,
    normalized_quote,
    plain_irc,
    reply_context,
    soup_for,
)
from site_chat import SiteChat, load_sticker_names
from sticker_server import StickerServer
from irc_metadata import NickMetadata, group_color

SERVER = "capybara.local"
CHANNEL = "#capybara"
CAPS = {"server-time", "echo-message", "message-tags", "batch", "draft/metadata-2", "standard-replies"}
CAP_VALUES = {"draft/metadata-2": "max-subs=6,max-keys=0,max-value-bytes=0"}
# Conta de sistema do UNIT3D; o site não permite responder a ela.
SYSTEM_USER_ID = 1
NICK_RE = re.compile(r"^[A-Za-z_\[\]\\`^{}|][A-Za-z0-9_\[\]\\`^{}|\-]{0,29}$")


def irc_fold(value):
    return value.lower().translate(str.maketrans("[]\\^", "{}|~"))


def safe(value):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value))


def parse_line(line):
    if line.startswith("@"):
        line = line.partition(" ")[2]
    if " :" in line:
        head, tail = line.split(" :", 1)
        parts = head.split() + [tail]
    else:
        parts = line.split()
    return (parts[0].upper(), parts[1:]) if parts else ("", [])


def parse_tags(line):
    if not line.startswith("@"):
        return {}
    values = {}
    escapes = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}
    for item in line[1:].partition(" ")[0].split(";"):
        key, _, value = item.partition("=")
        values[key] = re.sub(
            r"\\(.)|\\$", lambda m: escapes.get(m[1], m[1]) if m[1] else "", value
        )
    return values


def escape_tag(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\:")
        .replace(" ", "\\s")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def split_utf8(text, limit):
    chunk = ""
    size = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            yield chunk
            chunk, size = "", 0
        chunk += char
        size += width
    if chunk:
        yield chunk


def formatted_chunks(text, limit):
    """Não separar parâmetros de cor; reabrir estilos nos fragmentos seguintes."""
    active = []
    color = ""
    chunk = ""
    tokens = []
    start = 0
    for match in CONTROL.finditer(text):
        tokens.extend(text[start : match.start()])
        tokens.append(match[0])
        start = match.end()
    tokens.extend(text[start:])
    for token in tokens:
        # Reservar um RESET para cada linha, sem vazar estilo ao próximo frame.
        if len((chunk + token).encode()) > limit - 1 and chunk:
            yield chunk + "\x0f"
            chunk = "".join(active) + color
        chunk += token
        if token == "\x0f":
            active, color = [], ""
        elif token[0] in ("\x03", "\x04"):
            color = token if len(token) > 1 else ""
        elif token in ("\x02", "\x1d", "\x1f", "\x1e", "\x11", "\x16"):
            active.remove(token) if token in active else active.append(token)
    if chunk:
        yield chunk + ("\x0f" if active or color else "")


class Client:
    def __init__(self, bridge, reader, writer):
        self.bridge, self.reader, self.writer = bridge, reader, writer
        self.nick = "*"
        self.user = ""
        self.registered = False
        self.negotiating = False
        self.joined = False
        self.caps = set()
        self.roster = set()
        self.write_lock = asyncio.Lock()
        self.delivered = set()
        self.metadata = NickMetadata(self, SERVER, CHANNEL)

    @property
    def prefix(self):
        return f"{self.nick}!local@localhost"

    async def write(self, line):
        if self.writer.is_closing():
            return
        # Todo frame deve respeitar os 512 bytes do IRC, inclusive CRLF.
        data = (line + "\r\n").encode("utf-8")
        if len(data) > 512:
            raise ValueError("Frame IRC excedeu 512 bytes")
        async with self.write_lock:
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), 10)

    async def numeric(self, number, text):
        await self.write(f":{SERVER} {number:03d} {self.nick} {text}")

    async def topic(self):
        text, setter, stamp = self.bridge.topic()
        await self.numeric(332, f"{CHANNEL} :{text}")
        if setter:
            await self.numeric(333, f"{CHANNEL} {setter} {stamp}")

    async def notice(self, text, channel=False):
        # Avisos ao nick vão para a aba do servidor; ao canal, ficam na conversa.
        target = CHANNEL if channel and self.joined else self.nick
        prefix = f":{SERVER} NOTICE {target} :"
        for chunk in split_utf8(safe(text), 510 - len(prefix.encode())):
            await self.write(prefix + chunk)

    async def welcome(self):
        if self.registered or not self.user or self.nick == "*" or self.negotiating:
            return
        self.registered = True
        await self.numeric(1, f":Bem-vindo ao bridge CapybaraBR, {self.nick}")
        await self.numeric(2, f":Seu servidor é {SERVER}")
        await self.numeric(3, ":Servidor IRC local")
        await self.numeric(4, f"{SERVER} 1.0 i n")
        await self.numeric(
            5,
            "CHANTYPES=# CASEMAPPING=rfc1459 NICKLEN=30 CHANMODES=,,,n PREFIX=(ov)@+ CLIENTTAGDENY=*,-draft/react,-draft/unreact,-draft/reply,-reply,-typing :are supported",
        )
        await self.numeric(375, ":- CapybaraBR IRC bridge -")
        await self.numeric(
            372,
            f":Mensagens em {CHANNEL} são publicadas como {self.bridge.site.user['username']}.",
        )
        await self.metadata.sync(self.nick, registration=True)
        await self.numeric(376, ":End of MOTD")
        await self.bridge.replay_private(self)

    async def names(self):
        names = sorted(self.bridge.names(self), key=irc_fold)
        prefix = f":{SERVER} 353 {self.nick} = {CHANNEL} :"
        batch = ""
        for name in names:
            if len((prefix + batch + " " + name).encode()) > 510:
                await self.write(prefix + batch)
                batch = ""
            batch = (batch + " " + name).strip()
        if batch:
            await self.write(prefix + batch)
        await self.numeric(366, f"{CHANNEL} :End of NAMES list")

    async def message(self, message, history=False, origin=None):
        user = message.get("user") or message.get("bot") or {}
        is_self = user.get("id") == self.bridge.site.user["id"] and bool(
            message.get("user")
        )
        nick = self.nick if is_self else self.bridge.site_nick(user)
        private = SiteChat.is_private(message)
        target = CHANNEL
        if private:
            # Recebida: a query é endereçada ao cliente. Enviada pela conta
            # (inclusive pelo navegador): aparece na query do destinatário.
            peer = self.bridge.site.peer(message)
            target = (
                self.bridge.site_nick(peer)
                if is_self and peer.get("id") != self.bridge.site.user["id"]
                else self.nick
            )
        text = html_to_irc(message.get("message", ""), self.bridge.site.base_url)
        native = message.get("reply_to")
        native = native if isinstance(native, dict) else None
        # Mensagens anteriores ao reply nativo ainda trazem a citação no HTML.
        reply = None if native or private else extract_reply(
            message.get("message", ""), self.bridge.site.base_url
        )
        parent = self.bridge.reply_parent(message, reply)
        threaded = parent in self.delivered and "message-tags" in self.caps
        if reply and threaded:
            text = reply.body or "(citação)"
        elif native and not threaded:
            text = reply_context(native) + ("\n" + text if text else "")
        sticker = message.get("sticker")
        if isinstance(sticker, dict):
            sid = self.bridge.site.sticker_id(sticker.get("url"))
            name = (
                self.bridge.site.sticker_names.get(sid)
                or sticker.get("name")
                or "sem nome"
            )
            label = "[figurinha: " + safe(name) + "]"
            if sid is not None:
                label += " " + self.bridge.sticker_url(sid)
            text = (text + " " if text else "") + label
        stamp = ""
        timestamp = message.get("created_at", "")
        if "server-time" in self.caps:
            try:
                time = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if time.tzinfo:
                    stamp = (
                        "@time="
                        + time.astimezone(datetime.timezone.utc)
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z")
                        + " "
                    )
            except (ValueError, AttributeError):
                pass
        if history and not stamp:
            text = "[histórico] " + text
        if not history and origin is self and "echo-message" not in self.caps:
            return
        if nick not in self.roster and not is_self and not private:
            await self.write(f":{nick}!site@capybarabr.com JOIN {CHANNEL}")
            self.roster.add(nick)
        if 'group' in user:
            await self.metadata.color(nick, group_color(user))
        tags = stamp[1:].strip() if stamp else ""
        if "message-tags" in self.caps:
            tags = (
                (tags + ";" if tags else "")
                + "msgid="
                + self.bridge.message_id(message["id"], private)
            )
            if parent in self.delivered:
                reference = self.bridge.message_id(parent, private)
                tags += f";+reply={reference};+draft/reply={reference}"
        stamp = "@" + tags + " " if tags else ""
        prefix = f"{stamp}:{nick}!site@capybarabr.com PRIVMSG {target} :"
        chunk_number = 0
        # splitlines() também divide no controle IRC de tachado (0x1e).
        for line in text.split("\n") or ["(mensagem sem texto)"]:
            for chunk in formatted_chunks(
                clean_irc(line), 510 - len(prefix.encode("utf-8"))
            ):
                # Apenas o primeiro fragmento representa o ID usado por reações.
                fragment_prefix = prefix
                if chunk_number and "message-tags" in self.caps:
                    fragment_tags = ";".join(
                        tag for tag in tags.split(";")
                        if tag and tag.partition("=")[0] != "msgid"
                    )
                    fragment_stamp = "@" + fragment_tags + " " if fragment_tags else ""
                    fragment_prefix = f"{fragment_stamp}:{nick}!site@capybarabr.com PRIVMSG {target} :"
                await self.write(fragment_prefix + chunk)
                chunk_number += 1
        self.delivered.add(int(message["id"]))
        for emoji, username in sorted(SiteChat.reaction_users(message)):
            await self.reaction(int(message["id"]), emoji, username)

    async def reaction(self, mid, emoji, username, remove=False):
        if mid not in self.delivered:
            return
        if "message-tags" not in self.caps:
            await self.notice(
                f"{username} {'removeu' if remove else 'adicionou'} {emoji} na mensagem #{mid}."
            )
            return
        nick = (
            self.nick
            if username == self.bridge.site.user["username"]
            else self.bridge.reaction_nick(username)
        )
        tag = "+draft/unreact" if remove else "+draft/react"
        reference = self.bridge.message_id(mid)
        tags = f"+reply={reference};+draft/reply={reference};{tag}={escape_tag(emoji)}"
        await self.write(f"@{tags} :{nick}!site@capybarabr.com TAGMSG {CHANNEL}")

    async def react_command(self, args, tags):
        if (
            "message-tags" not in self.caps
            or not self.joined
            or not args
            or irc_fold(args[0]) != CHANNEL
        ):
            await self.notice("Reações exigem message-tags e entrada em #capybara.")
            return
        react, unreact = tags.get("+draft/react"), tags.get("+draft/unreact")
        reply = tags.get("+reply") or tags.get("+draft/reply")
        match = re.fullmatch(rf"cbr-{self.bridge.site.room}-(\d+)", reply or "")
        emoji = react if react is not None else unreact
        if (
            (react is None) == (unreact is None)
            or not match
            or not emoji
            or len(emoji.encode("utf-8")) > 128
            or safe(emoji) != emoji
        ):
            await self.notice(
                "Reação inválida: informe emoji e ID de uma mensagem recebida."
            )
            return
        mid = int(match[1])
        if mid not in self.delivered or mid not in self.bridge.site.history:
            await self.notice("Mensagem não disponível no histórico desta sessão.")
            return
        try:
            await self.bridge.site.set_reaction(mid, emoji, remove=unreact is not None)
        except Exception as exc:
            await self.notice(
                f"Reação não confirmada ({type(exc).__name__}); confira o site antes de tentar novamente."
            )

    async def outgoing_text(self, text):
        # A URL de uma figurinha, sozinha, volta ao site como figurinha.
        sid = self.bridge.sticker_from_url(plain_irc(text).strip())
        if sid is not None and await self.bridge.site.sticker_matches(str(sid)):
            return f"[sticker]{sid}[/sticker]"
        return irc_to_bbcode(text)

    async def sticker_command(self, query):
        def say(text):
            return self.notice(text, channel=True)

        if not self.joined:
            await say(f"Entre em {CHANNEL} para enviar figurinhas.")
            return
        site = self.bridge.site
        matches = await site.sticker_matches(query)
        if query and len(matches) == 1:
            try:
                await site.send(f"[sticker]{matches[0]['id']}[/sticker]", origin=self)
            except Exception as exc:
                await say(
                    f"Envio não confirmado ({type(exc).__name__}). Confira o chat do site antes de reenviar."
                )
            return
        if not site.stickers:
            await say("Nenhuma figurinha disponível no site.")
            return
        if query and not matches:
            await say(f"Nenhuma figurinha corresponde a “{query}”.")
            matches = list(site.stickers.values())
        elif query:
            await say("Mais de uma figurinha corresponde; use o número.")
        packs = {}
        for sticker in matches:
            packs.setdefault(sticker["pack"], []).append(sticker)
        for pack, stickers in packs.items():
            await say(
                (pack or "Figurinhas") + ": "
                + ", ".join(f"{s['id']} {s['name']}" for s in stickers)
            )
        await say("Envie com /fig <número ou nome>.")

    async def command(self, command, args, tags=None):
        tags = tags or {}
        if command == "CAP":
            sub = args[0].upper() if args else ""
            if sub == "LS":
                self.negotiating = not self.registered
                await self.write(
                    f":{SERVER} CAP {self.nick} LS :" + " ".join(c + ('=' + CAP_VALUES[c] if c in CAP_VALUES else '') for c in sorted(CAPS))
                )
            elif sub == "REQ" and len(args) > 1:
                requested = args[-1].split()
                valid = all(c.lstrip("-") in CAPS for c in requested)
                if valid:
                    for cap in requested:
                        if cap.startswith("-"):
                            self.caps.discard(cap[1:])
                        else:
                            self.caps.add(cap)
                await self.write(
                    f":{SERVER} CAP {self.nick} {'ACK' if valid else 'NAK'} :{safe(args[-1])}"
                )
                if valid and self.registered and self.metadata.enabled:
                    await self.metadata.sync(self.nick, registration=True)
            elif sub == "LIST":
                await self.write(
                    f":{SERVER} CAP {self.nick} LIST :" + " ".join(sorted(self.caps))
                )
            elif sub == "END":
                self.negotiating = False
                await self.welcome()
            return
        if command == "NICK" and args:
            nick = args[0]
            if not NICK_RE.fullmatch(nick):
                await self.numeric(432, f"{safe(nick)[:30]} :Nickname inválido")
            elif self.bridge.nick_in_use(nick, self):
                await self.numeric(433, f"{nick} :Nickname em uso")
            else:
                old = self.prefix
                self.nick = nick
                if self.registered:
                    await self.write(f":{old} NICK :{nick}")
                await self.welcome()
            return
        if command == "USER" and args:
            self.user = safe(args[0])[:30]
            await self.welcome()
            return
        if command == "PING":
            await self.write(
                f":{SERVER} PONG {SERVER} :{safe(args[-1] if args else SERVER)}"
            )
            return
        if command in ("PONG", "PASS"):
            return
        if not self.registered:
            await self.numeric(451, ":You have not registered")
            return
        if command == 'METADATA':
            await self.metadata.command(args)
            return
        if command in ("TAGMSG", "PRIVMSG") and any(
            t in tags for t in ("+draft/react", "+draft/unreact")
        ):
            await self.react_command(args, tags)
            return
        if command == "TAGMSG":
            if 'message-tags' in self.caps and self.joined and args and irc_fold(args[0]) == CHANNEL:
                status = tags.get('+typing')
                if status in ('active', 'paused', 'done'):
                    await self.bridge.site.send_typing(status)
            return
        if command == "JOIN" and args:
            if irc_fold(args[0]) != CHANNEL:
                await self.numeric(
                    403, f"{safe(args[0])[:40]} :Canal inexistente; use {CHANNEL}"
                )
            elif not self.joined:
                self.joined = True
                await self.write(f":{self.prefix} JOIN {CHANNEL}")
                await self.topic()
                self.roster = self.bridge.names(self)
                await self.names()
                await self.metadata.sync()
                await self.notice(
                    f"Conta do site: {self.bridge.site.user['username']}. Apenas mensagens deste canal são enviadas ao site. Figurinhas: /fig."
                )
                history = (
                    list(self.bridge.site.history.values())[
                        -self.bridge.history_count :
                    ]
                    if self.bridge.history_count
                    else []
                )
                for message in history:
                    await self.message(message, history=True)
        elif command == "PART" and args:
            if self.joined and irc_fold(args[0]) == CHANNEL:
                await self.write(f":{self.prefix} PART {CHANNEL} :Saindo")
                self.joined = False
                self.roster.clear()
        elif command == "PRIVMSG" and len(args) >= 2:
            receiver = None
            if irc_fold(args[0]) != CHANNEL:
                receiver = self.bridge.user_for_nick(args[0], self)
                if receiver is None:
                    await self.numeric(
                        401,
                        f"{safe(args[0])[:40]} :Usuário desconhecido; ele precisa estar online ou ter conversa aberta no site",
                    )
                    return
            elif not self.joined:
                await self.numeric(
                    404,
                    f"{safe(args[0])[:40]} :Entre em {CHANNEL} para enviar mensagens",
                )
                return
            text = args[1]
            if text.startswith("\x01ACTION ") and text.endswith("\x01"):
                text = "* " + text[8:-1]
            elif "\x01" in text:
                await self.notice("CTCP não é enviado ao site.")
                return
            if text.strip():
                try:
                    reference = tags.get("+reply") or tags.get("+draft/reply")
                    parent = None
                    if reference:
                        kind = "dm" if receiver else str(self.bridge.site.room)
                        match = re.fullmatch(rf"cbr-{kind}-(\d+)", reference)
                        if "message-tags" not in self.caps or not match:
                            await self.notice(
                                "Reply inválido: selecione uma mensagem recebida nesta conversa."
                            )
                            return
                        parent = int(match[1])
                        if receiver:
                            original = self.bridge.site.private_history.get(parent)
                            if original and self.bridge.site.peer(original).get("id") != receiver["id"]:
                                original = None
                        else:
                            original = self.bridge.site.history.get(parent)
                        if parent not in self.delivered or original is None:
                            await self.notice(
                                "Mensagem original não disponível; resposta não enviada."
                            )
                            return
                        user = original.get("user") or {}
                        if not user or user.get("id") == SYSTEM_USER_ID:
                            await self.notice(
                                "O site não permite responder a mensagens do sistema ou de bots."
                            )
                            return
                        nick = (
                            self.nick
                            if user.get("id") == self.bridge.site.user["id"]
                            else self.bridge.site_nick(user)
                        )
                        # O Halloy acrescenta "nick: " ao corpo mesmo com +reply.
                        # O site já mostra o autor no reply nativo.
                        names = {nick, user.get("username") or user.get("name") or nick}
                        pattern = (
                            r"^(?:"
                            + "|".join(re.escape(name) for name in names)
                            + r"):\s+"
                        )
                        text = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)
                        text = await self.outgoing_text(text)
                        if not text:
                            await self.notice("Resposta vazia; mensagem não enviada.")
                            return
                    else:
                        text = await self.outgoing_text(text)
                    await self.bridge.site.send(
                        text,
                        origin=self,
                        reply_to=parent,
                        receiver=receiver["id"] if receiver else None,
                    )
                except Exception as exc:
                    await self.notice(
                        f"Envio não confirmado ({type(exc).__name__}). Confira o chat do site antes de reenviar."
                    )
        elif command in ("FIG", "FIGURINHA", "STICKER"):
            await self.sticker_command(" ".join(args).strip())
        elif command == "NAMES":
            await self.names()
        elif command == "TOPIC":
            if len(args) > 1:
                await self.numeric(
                    482, f"{CHANNEL} :Só moderadores fixam mensagens, pelo site"
                )
            else:
                await self.topic()
        elif command == "MODE" and args:
            if irc_fold(args[0]) == CHANNEL:
                await self.numeric(324, f"{CHANNEL} +n")
            else:
                await self.numeric(221, "+i")
        elif command == "WHO":
            for nick in sorted(self.bridge.names(self)):
                await self.numeric(
                    352, f"{CHANNEL} site capybarabr.com {SERVER} {nick} H :0 {nick}"
                )
            await self.numeric(315, f"{CHANNEL} :End of WHO list")
        elif command == "LIST":
            await self.numeric(321, "Channel :Users Name")
            await self.numeric(
                322,
                f"{CHANNEL} {len(self.bridge.names(self))} :{safe(self.bridge.site.room_name)}",
            )
            await self.numeric(323, ":End of LIST")
        elif command == "MOTD":
            await self.numeric(422, ":Use " + CHANNEL)
        elif command == "NOTICE":
            return
        else:
            await self.numeric(421, f"{safe(command)[:30]} :Comando não suportado")

    async def run(self):
        try:
            while line := await self.reader.readline():
                if len(line) > 512:
                    await self.notice("Linha IRC excedeu 512 bytes.")
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
                command, args = parse_line(decoded)
                if command == "QUIT":
                    break
                await self.command(command, args, parse_tags(decoded))
        except (ConnectionError, TimeoutError, ValueError):
            pass
        finally:
            self.bridge.clients.discard(self)
            self.writer.close()
            with contextlib.suppress(ConnectionError):
                await self.writer.wait_closed()


class IRCBridge:
    def __init__(self, site, history_count=20, stickers=None):
        self.site = site
        self.history_count = history_count
        self.stickers = stickers
        self.clients = set()
        self.tasks = set()
        self.nick_map = {}
        self.private_mark = 0
        site.on_message = self.broadcast
        site.on_private = self.private
        site.on_presence = self.presence
        site.on_notice = self.notice
        site.on_reaction = self.reactions
        site.on_user = self.user_metadata
        site.on_typing = self.typing
        site.on_pin = self.pin

    def topic(self):
        """Tópico do canal: nome da sala e a mensagem fixada, se houver."""
        text = f"CapybaraBR / {self.site.room_name}"
        pinned = self.site.pinned
        if not pinned:
            return safe(text), None, None
        author = (pinned.get("user") or {}).get("username") or "?"
        excerpt = " ".join(str(pinned.get("excerpt") or "Imagem ou mídia").split())
        if len(excerpt) > 300:
            excerpt = excerpt[:299] + "…"
        setter = self.reaction_nick(pinned.get("pinned_by") or author)
        try:
            stamp = int(datetime.datetime.fromisoformat(
                str(pinned.get("pinned_at")).replace("Z", "+00:00")
            ).timestamp())
        except ValueError:
            stamp = int(datetime.datetime.now().timestamp())
        return safe(f"{text} | 📌 {author}: {excerpt}"), setter, stamp

    async def pin(self, pinned):
        text, setter, _ = self.topic()
        source = f"{setter}!site@capybarabr.com" if setter else SERVER

        await self.fanout(lambda c: c.write(f":{source} TOPIC {CHANNEL} :{text}"))

    async def typing(self, username):
        if username == self.site.user['username']:
            return
        nick = self.reaction_nick(username)
        async def update(client):
            if 'message-tags' in client.caps and username != self.site.user['username']:
                await client.write(f'@+typing=active :{nick}!site@capybarabr.com TAGMSG {CHANNEL}')
        await self.fanout(update)

    async def user_metadata(self, user):
        async def update(client):
            nick = client.nick if user.get('id') == self.site.user['id'] else self.site_nick(user)
            await client.metadata.color(nick, group_color(user))
        await self.fanout(update)

    def sticker_url(self, sid):
        if self.stickers:
            return self.stickers.url(sid)
        return f"{self.site.base_url}/authenticated-images/stickers/{sid}"

    def sticker_from_url(self, text):
        sid = self.site.sticker_id(text)
        if sid is None and self.stickers:
            match = re.fullmatch(re.escape(self.stickers.url("")) + r"(\d{1,9})", text)
            sid = int(match[1]) if match else None
        return sid

    def message_id(self, mid, private=False):
        return f"cbr-{'dm' if private else self.site.room}-{mid}"

    def user_for_nick(self, nick, client):
        """Usuário do site correspondente a um nick de query."""
        me = self.site.user
        if irc_fold(nick) in (irc_fold(client.nick), irc_fold(me.get("username", ""))):
            return me
        candidates = [*self.site.members.values(), *self.site.targets.values()]
        for message in [*self.site.history.values(), *self.site.private_history.values()]:
            candidates += [message.get("user"), message.get("receiver")]
        for user in candidates:
            if (
                isinstance(user, dict)
                and user.get("id")
                and user.get("username")
                and user["id"] != me["id"]
                and irc_fold(self.site_nick(user)) == irc_fold(nick)
            ):
                return user
        return None

    async def private(self, message, origin=None):
        if any(c.registered for c in self.clients):
            self.private_mark = max(self.private_mark, int(message["id"]))
        await self.fanout(lambda c: c.message(message, origin=origin), registered=True)

    async def replay_private(self, client):
        # DMs recebidas sem nenhum cliente conectado, ainda no cache do bridge.
        pending = [
            m for mid, m in self.site.private_history.items() if mid > self.private_mark
        ]
        for message in pending[-max(self.history_count, 1):]:
            await client.message(message, history=True)
        if pending:
            self.private_mark = max(self.private_mark, int(pending[-1]["id"]))

    def reply_parent(self, message, reply):
        if message.get("_reply_to") is not None:
            return message["_reply_to"]
        native = message.get("reply_to")
        if isinstance(native, dict) and native.get("id"):
            with contextlib.suppress(TypeError, ValueError):
                return int(native["id"])
            return None
        if not reply or not normalized_quote(reply.quoted):
            return None
        matches = []
        for mid, candidate in self.site.history.items():
            if mid >= int(message["id"]):
                continue
            user = candidate.get("user") or candidate.get("bot") or {}
            author = user.get("username") or user.get("name") or "Sistema"
            if author.casefold() != reply.author.casefold():
                continue
            soup = soup_for(candidate.get("message", ""))
            for quote in soup.find_all("blockquote"):
                quote.decompose()
            body = html_to_irc(str(soup), self.site.base_url, styled=False)
            if normalized_quote(body) == normalized_quote(reply.quoted):
                matches.append(mid)
        return matches[0] if len(matches) == 1 else None

    def reaction_nick(self, username):
        for key, nick in self.nick_map.items():
            if key.partition(":")[2] == username:
                return nick
        user = next(
            (u for u in self.site.members.values() if u.get("username") == username),
            None,
        )
        if user is None:
            user = next(
                (
                    m["user"]
                    for m in self.site.history.values()
                    if (m.get("user") or {}).get("username") == username
                ),
                None,
            )
        return self.site_nick(user or {"id": "reaction", "username": username})

    async def reactions(self, mid, added, removed):
        for remove, changes in ((True, removed), (False, added)):
            for emoji, username in sorted(changes):
                await self.fanout(lambda c: c.reaction(mid, emoji, username, remove))

    def site_nick(self, user):
        key = (
            str(user.get("id", "system"))
            + ":"
            + str(user.get("username") or user.get("name") or "Sistema")
        )
        if key not in self.nick_map:
            raw = user.get("username") or user.get("name") or "Sistema"
            nick = re.sub(r"[^A-Za-z0-9_\-]", "_", str(raw))[:24] or "usuario"
            if not re.match("[A-Za-z_]", nick):
                nick = "u_" + nick
            taken = {irc_fold(n) for n in self.nick_map.values()} | {
                irc_fold(c.nick) for c in self.clients
            }
            if irc_fold(nick) in taken:
                nick += "_" + hashlib.sha256(key.encode()).hexdigest()[:5]
            self.nick_map[key] = nick
        return self.nick_map[key]

    def nick_in_use(self, nick, client):
        return any(
            irc_fold(c.nick) == irc_fold(nick) for c in self.clients if c is not client
        ) or any(irc_fold(n) == irc_fold(nick) for n in self.nick_map.values())

    def names(self, client):
        return {client.nick} | {
            self.site_nick(u)
            for uid, u in self.site.members.items()
            if uid != self.site.user["id"]
        }

    async def fanout(self, callback, registered=False):
        clients = [c for c in self.clients if (c.registered if registered else c.joined)]
        results = await asyncio.gather(
            *(callback(c) for c in clients), return_exceptions=True
        )
        for client, result in zip(clients, results):
            if isinstance(result, BaseException):
                client.writer.close()

    async def broadcast(self, message, origin=None):
        await self.fanout(lambda c: c.message(message, origin=origin))

    async def notice(self, text):
        await self.fanout(lambda c: c.notice(text))

    async def presence(self):
        async def update(client):
            names = self.names(client)
            for nick in sorted(names - client.roster):
                await client.write(f":{nick}!site@capybarabr.com JOIN {CHANNEL}")
            for nick in sorted(client.roster - names):
                if nick != client.nick:
                    await client.write(
                        f":{nick}!site@capybarabr.com PART {CHANNEL} :Saiu do chat"
                    )
            client.roster = names
            if client.metadata.enabled:
                for nick, color in client.metadata.users().items():
                    await client.metadata.color(nick, color)

        await self.fanout(update)

    async def accept(self, reader, writer):
        client = Client(self, reader, writer)
        self.clients.add(client)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            await client.run()
        finally:
            self.tasks.discard(task)


async def run(args):
    site = SiteChat(
        args.cookies, args.room, sticker_names=load_sticker_names(args.sticker_names)
    )
    tasks = []
    bridge = None
    try:
        await site.initialize()
        print(
            f"Autenticado como {site.user['username']}; sala {site.room_name}.",
            flush=True,
        )
        if args.check:
            task = asyncio.create_task(site.websocket())
            tasks.append(task)
            await asyncio.wait_for(site.connected.wait(), 30)
            sample = next(iter(site.stickers), None)
            image = await site.sticker_image(sample) if sample is not None else None
            print(
                f"HTTP e WebSocket OK; {len(site.history)} mensagens, {len(site.members)} usuários online, "
                f"{len(site.stickers)} figurinhas ({'imagem OK' if image else 'imagem indisponível'}).",
                flush=True,
            )
            return
        stickers = StickerServer(site, args.http_port, args.sticker_base_url)
        bridge = IRCBridge(site, args.history, stickers)
        async with await asyncio.start_server(
            stickers.handle, args.bind, args.http_port, limit=4096
        ) as http_server, await asyncio.start_server(
            bridge.accept, args.bind, args.port, limit=4096
        ) as server:
            tasks = [
                asyncio.create_task(site.websocket()),
                asyncio.create_task(site.poll()),
            ]
            print(
                f"IRC em {args.bind}:{args.port}, sem TLS; canal {CHANNEL}. "
                f"Figurinhas em {args.bind}:{args.http_port}/. Ctrl+C encerra.",
                flush=True,
            )
            await asyncio.gather(server.serve_forever(), http_server.serve_forever())
    finally:
        if bridge:
            tasks += list(bridge.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await site.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cookies", type=Path, default=Path(__file__).with_name("capybarabr.txt")
    )
    parser.add_argument(
        "--sticker-names",
        type=Path,
        default=Path(__file__).with_name("figurinhas.json"),
        help="Nomes locais das figurinhas, em JSON",
    )
    parser.add_argument("--port", type=int, default=6667)
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Endereço de escuta dos servidores IRC e HTTP (padrão: 127.0.0.1)",
    )
    parser.add_argument(
        "--http-port", type=int, help="Porta HTTP das figurinhas (padrão: --port + 1)"
    )
    parser.add_argument(
        "--sticker-base-url",
        help="URL anunciada para as figurinhas (padrão: http://127.0.0.1:<porta HTTP>)",
    )
    parser.add_argument("--room", type=int, default=1)
    parser.add_argument("--history", type=int, default=20)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verificar autenticação e WebSocket sem publicar mensagens",
    )
    args = parser.parse_args()
    if args.http_port is None:
        args.http_port = args.port + 1
    if (
        not 0 <= args.history <= 100
        or not 1 <= args.port <= 65535
        or not 1 <= args.http_port <= 65535
        or args.http_port == args.port
    ):
        parser.error(
            "--history deve estar entre 0 e 100; --port e --http-port entre 1 e 65535, diferentes"
        )
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    # As versões legadas emitem frames completos se herdarem INFO do root.
    # Esses frames contêm headers de autenticação; nunca registrá-los.
    for name in ("socketio", "engineio", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        parser.exit(1, f"Falha: {type(exc).__name__}: {safe(str(exc))}\n")


if __name__ == "__main__":
    main()
