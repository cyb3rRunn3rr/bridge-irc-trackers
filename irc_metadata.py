"""Metadata IRCv3 de leitura: a cor do nickname vem do grupo no site."""
import re


def group_color(user):
    value = (user.get('group') or {}).get('color')
    return value.upper() if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value) else None


class NickMetadata:
    def __init__(self, client, server, channel):
        self.client = client
        self.server = server
        self.channel = channel
        self.subscriptions = set()
        self.sent = {}
        self.batch_id = 0

    @property
    def enabled(self):
        return {'batch', 'draft/metadata-2'} <= self.client.caps

    def users(self):
        bridge = self.client.bridge
        site = bridge.site
        users = {self.client.nick: group_color(site.user)}
        for message in site.history.values():
            user = message.get('user') or message.get('bot') or {}
            nick = self.client.nick if message.get('user') and user.get('id') == site.user['id'] else bridge.site_nick(user)
            users[nick] = group_color(user)
        for user in site.members.values():
            nick = self.client.nick if user.get('id') == site.user['id'] else bridge.site_nick(user)
            users[nick] = group_color(user)
        return users

    async def color(self, nick, color, force=False):
        if not self.enabled or ('color' not in self.subscriptions and not force):
            return
        if not force and self.sent.get(nick) == color:
            return
        if color is None and nick not in self.sent:
            return
        self.sent[nick] = color
        suffix = ' :' + color if color else ''
        await self.client.write(f':{self.server} METADATA {nick} color *{suffix}')

    async def sync(self, target=None, registration=False):
        if not self.enabled:
            return
        target = target or self.channel
        users = self.users()
        targets = users if target == self.channel else {target: users.get(target)}
        rows = []
        if registration or 'color' in self.subscriptions:
            for nick, color in targets.items():
                if color:
                    rows.append(f':{self.server} METADATA {nick} color * :{color}')
                    self.sent[nick] = color
                elif nick in self.sent and self.sent[nick] is not None:
                    rows.append(f':{self.server} METADATA {nick} color *')
                    self.sent[nick] = None
        await self.batch('metadata', target, rows)

    async def batch(self, kind, target, rows):
        self.batch_id += 1
        bid = f'md{self.batch_id}'
        extra = ' ' + target if target else ''
        await self.client.write(f':{self.server} BATCH +{bid} {kind}{extra}')
        for row in rows:
            await self.client.write(f'@batch={bid} {row}')
        await self.client.write(f':{self.server} BATCH -{bid}')

    async def fail(self, code, context, description):
        # Não repetir texto arbitrário do cliente nos frames de resposta.
        context = re.sub(r'[\x00-\x1f\x7f]', '_', context)[:80]
        await self.client.write(f':{self.server} FAIL METADATA {code} {context} :{description}')

    async def command(self, args):
        if not self.enabled:
            await self.fail('CAPABILITY_REQUIRED', '*', 'Negotiate batch and draft/metadata-2')
            return
        if len(args) < 2:
            await self.fail('SUBCOMMAND_INVALID', '*', 'Missing subcommand')
            return
        target, sub = args[0], args[1].upper()
        if target == '*':
            target = self.client.nick
        params = args[2:]
        if sub in ('SUB', 'UNSUB'):
            if target != self.client.nick:
                await self.fail('INVALID_TARGET', target, 'Use * for subscriptions')
                return
            for key in params:
                if not re.fullmatch(r'[a-z0-9_./-]+', key):
                    await self.fail('KEY_INVALID', key, 'Invalid key')
                    continue
                if sub == 'SUB':
                    if key not in self.subscriptions and len(self.subscriptions) >= 6:
                        await self.fail('TOO_MANY_SUBS', key, 'Maximum 6 subscriptions')
                        break
                    self.subscriptions.add(key)
                else:
                    self.subscriptions.discard(key)
                await self.client.numeric(770 if sub == 'SUB' else 771, key)
            if sub == 'SUB':
                await self.sync(self.channel if self.client.joined else self.client.nick)
        elif sub == 'SUBS':
            rows = [f':{self.server} 772 {self.client.nick} {key}' for key in sorted(self.subscriptions)]
            await self.batch('metadata-subs', '', rows)
        elif sub in ('SET', 'CLEAR'):
            await self.fail('KEY_NO_PERMISSION', target + ' ' + (params[0] if params else '*'), 'Metadata comes from the site and is read-only')
        elif sub in ('GET', 'LIST', 'SYNC'):
            users = self.users()
            if target != self.channel:
                target = next((nick for nick in users if nick.lower() == target.lower()), target)
            if target not in users and target != self.channel:
                await self.fail('INVALID_TARGET', target, 'Unknown nickname/channel')
                return
            if sub == 'SYNC':
                await self.sync(target)
                return
            keys = params if sub == 'GET' else (['color'] if users.get(target) else [])
            rows = []
            for key in keys:
                if not re.fullmatch(r'[a-z0-9_./-]+', key):
                    rows.append(f':{self.server} FAIL METADATA KEY_INVALID invalid :Invalid key')
                elif key == 'color' and users.get(target):
                    rows.append(f':{self.server} 761 {self.client.nick} {target} color * :{users[target]}')
                else:
                    rows.append(f':{self.server} 766 {self.client.nick} {target} {key} :Key not set')
            await self.batch('metadata', target, rows)
        else:
            await self.fail('SUBCOMMAND_INVALID', sub, 'Unknown subcommand')
