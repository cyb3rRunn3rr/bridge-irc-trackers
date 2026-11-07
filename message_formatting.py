"""Conversão BBCode/HTML ↔ formatação IRC, sem buscar URLs de terceiros."""

import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString

ATTRIBUTES = {"\x02": "b", "\x1d": "i", "\x1f": "u", "\x1e": "s", "\x11": "code"}
PALETTE = [
    "FFFFFF",
    "000000",
    "00007F",
    "009300",
    "FF0000",
    "7F0000",
    "9C009C",
    "FC7F00",
    "FFFF00",
    "00FC00",
    "009393",
    "00FFFF",
    "0000FC",
    "FF00FF",
    "7F7F7F",
    "D2D2D2",
]
URL = re.compile(r"https?://[^\s<>\[\]\x00-\x1f]+", re.IGNORECASE)
CONTROL = re.compile(
    r"\x03(?:\d{1,2}(?:,\d{1,2})?)?|\x04(?:[0-9a-fA-F]{6}(?:,[0-9a-fA-F]{6})?)?|[\x02\x0f\x11\x16\x1d\x1e\x1f]"
)


def plain_irc(text):
    return CONTROL.sub("", text)


def clean_irc(text):
    # Preservar somente os controles de formatação, nunca CTCP/CR/NUL.
    return re.sub(
        r"[\x00-\x01\x05-\x09\x0b-\x0e\x10\x12-\x15\x17-\x1c\x7f]", "", str(text)
    )


def is_image_url(url):
    try:
        parsed = urlsplit(url)
        paths = [unquote(parsed.path)]
        for key in ("url", "src", "image"):
            paths += [
                urlsplit(unquote(v)).path for v in parse_qs(parsed.query).get(key, [])
            ]
        return any(
            re.search(r"\.(?:png|jpe?g|gif|webp|avif|bmp|svg)$", path, re.IGNORECASE)
            for path in paths
        )
    except ValueError:
        return False


def image_bbcode(text):
    protected = []
    output = []
    for part in re.split(r"(\[/?[a-z*]+(?:=[^\]]*)?\])", text, flags=re.IGNORECASE):
        tag = re.fullmatch(r"\[(/?)([a-z*]+)(?:=[^\]]*)?\]", part, re.IGNORECASE)
        if tag:
            close, name = tag[1], tag[2].lower()
            if name in ("img", "url", "code", "quote"):
                if close:
                    if name in protected:
                        protected.remove(name)
                else:
                    protected.append(name)
            output.append(part)
        elif protected:
            output.append(part)
        else:

            def wrap(match):
                raw = match[0]
                url = raw.rstrip(".,!?;:")
                while url.endswith(")") and url.count(")") > url.count("("):
                    url = url[:-1]
                suffix = raw[len(url) :]
                return ("[img]" + url + "[/img]" if is_image_url(url) else url) + suffix

            output.append(URL.sub(wrap, part))
    return "".join(output)


def irc_to_bbcode(text):
    text = image_bbcode(clean_irc(text))
    active = []
    output = []
    start = 0
    for match in CONTROL.finditer(text):
        output.append(text[start : match.start()])
        token = match[0]
        desired = list(active)
        if token[0] in ATTRIBUTES:
            tag = ATTRIBUTES[token[0]]
            desired.remove(tag) if tag in desired else desired.append(tag)
        elif token == "\x0f":
            desired = []
        elif token[0] in ("\x03", "\x04"):
            desired = [
                t for t in desired if not t.startswith("color=") and t != "spoiler"
            ]
            components = token[1:].split(",")
            color = components[0]
            if (
                len(components) == 2
                and components[0].lstrip("0").upper()
                == components[1].lstrip("0").upper()
            ):
                desired.append("spoiler")
                color = ""
            if color:
                if token[0] == "\x03":
                    number = int(color)
                    color = PALETTE[number] if number < len(PALETTE) else ""
                if color:
                    desired.append("color=#" + color.upper())
        # BBCode não possui background/reverse compatível com o chat.
        if desired != active:
            common = 0
            while (
                common < min(len(active), len(desired))
                and active[common] == desired[common]
            ):
                common += 1
            output.extend(
                "[/" + t.split("=")[0] + "]" for t in reversed(active[common:])
            )
            output.extend("[" + t + "]" for t in desired[common:])
            active = desired
        start = match.end()
    output.append(text[start:])
    output.extend("[/" + t.split("=")[0] + "]" for t in reversed(active))
    return "".join(output).strip()


def youtube_watch(value):
    value = value.strip()
    video = value
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
        try:
            url = urlsplit('https:' + value if value.startswith('//') else value)
            host = (url.hostname or '').lower()
            parts = url.path.strip('/').split('/')
            if host in ('youtu.be', 'www.youtu.be'):
                video = parts[0]
            elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtube-nocookie.com', 'www.youtube-nocookie.com'):
                video = parse_qs(url.query).get('v', [''])[0] if url.path == '/watch' else (parts[1] if len(parts) == 2 and parts[0] in ('embed', 'shorts', 'v') else '')
            else:
                return None
        except ValueError:
            return None
    return 'https://www.youtube.com/watch?v=' + video if re.fullmatch(r'[A-Za-z0-9_-]{11}', video) else None


def bbcode_html(raw):
    """Fallback para BBCode cru; a API normalmente retorna HTML pronto."""
    special = []

    def keep(value):
        special.append(value)
        return f"\ue000{len(special) - 1}\ue001"

    raw = re.sub(
        r"\[code\](.*?)\[/code\]",
        lambda m: keep("<code>" + html.escape(m[1]) + "</code>"),
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    raw = re.sub(
        r"\[img(?:=[^\]]*)?\](.*?)\[/img\]",
        lambda m: keep('<img src="' + html.escape(m[1].strip(), quote=True) + '">'),
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    result = html.escape(raw)
    result = re.sub(
        r'\[youtube\](.*?)\[/youtube\]',
        lambda m: f'<a href="{link}">{link}</a>' if (link := youtube_watch(html.unescape(m[1]))) else m[0],
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )
    paired = re.compile(
        r"\[(b|i|u|s|url|quote|color|size|font|center|spoiler)(?:=([^\]]*))?\]((?:(?!\[\1(?:=|\])).)*?)\[/\1\]",
        re.IGNORECASE | re.DOTALL,
    )

    def replace(match):
        name, arg, body = match[1].lower(), match[2], match[3]
        if name == "quote":
            return (
                "<blockquote>"
                + ("<cite>" + (arg or "") + "</cite>" if arg else "")
                + "<div>"
                + body
                + "</div></blockquote>"
            )
        if name == "url":
            return (
                '<a href="'
                + html.escape(html.unescape(arg or body), quote=True)
                + '">'
                + body
                + "</a>"
            )
        if name == "color":
            return (
                '<span style="color:'
                + html.escape(html.unescape(arg or ""), quote=True)
                + '">'
                + body
                + "</span>"
            )
        if name in ("size", "font", "center"):
            return body
        if name == "spoiler":
            return '<span class="spoiler">' + body + "</span>"
        return "<" + name + ">" + body + "</" + name + ">"

    for _ in range(20):
        result, count = paired.subn(replace, result)
        if not count:
            break
    for index, value in enumerate(special):
        result = result.replace(f"\ue000{index}\ue001", value)
    return result.replace("\n", "<br>")


def soup_for(raw):
    if not re.search(r"<[a-zA-Z][^>]*>", raw or "") and re.search(
        r"\[/?(?:b|i|u|s|img|url|quote|code|color|spoiler|youtube)(?:=|\])",
        raw or "",
        re.IGNORECASE,
    ):
        raw = bbcode_html(raw)
    return BeautifulSoup(raw or "", "html.parser")


def css_color(value):
    value = value.strip().lower()
    names = {
        "red": "FF0000",
        "green": "008000",
        "blue": "0000FF",
        "white": "FFFFFF",
        "black": "000000",
        "yellow": "FFFF00",
        "orange": "FFA500",
        "gray": "808080",
        "grey": "808080",
        "purple": "800080",
        "pink": "FFC0CB",
        "cyan": "00FFFF",
    }
    if value in names:
        return names[value]
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        return "".join(c * 2 for c in value[1:]).upper()
    if re.fullmatch(r"#[0-9a-f]{6}", value):
        return value[1:].upper()
    rgb = re.fullmatch(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*[\d.]+)?\)", value)
    if rgb and all(int(x) <= 255 for x in rgb.groups()):
        return "".join(f"{int(x):02X}" for x in rgb.groups())
    return ""


def render_html(node, base_url, styled=True, active=frozenset(), inherited_color=""):
    if isinstance(node, NavigableString):
        return clean_irc(str(node))
    name = node.name
    if name == 'iframe':
        link = youtube_watch(node.get('src', '') or node.get('data-src', ''))
        return '\n' + link + '\n' if link else ''
    if name in ("script", "style"):
        return ""
    if name == "br":
        return "\n"
    if name == "img":
        if "joypixels" in node.get("class", []):
            return node.get("alt", "")
        return " " + urljoin(base_url, node.get("src", "")) + " "
    toggles = {
        "b": "\x02",
        "strong": "\x02",
        "i": "\x1d",
        "em": "\x1d",
        "u": "\x1f",
        "s": "\x1e",
        "del": "\x1e",
        "strike": "\x1e",
        "code": "\x11",
        "pre": "\x11",
    }
    local = {toggles[name]} if name in toggles else set()
    style = dict(
        re.findall(r"([a-z-]+)\s*:\s*([^;]+)", node.get("style", ""), re.IGNORECASE)
    )
    if style.get("font-weight", "").strip() in ("bold", "700"):
        local.add("\x02")
    if "underline" in style.get("text-decoration", ""):
        local.add("\x1f")
    if "line-through" in style.get("text-decoration", ""):
        local.add("\x1e")
    if style.get("font-style", "").strip() == "italic":
        local.add("\x1d")
    color = css_color(style.get("color", "") or node.get("color", ""))
    toggle = "".join(sorted(local - active)) if styled else ""
    body = "".join(
        render_html(child, base_url, styled, active | local, color or inherited_color)
        for child in node.children
    )
    if name == "a":
        href = urljoin(base_url, node.get("href", ""))
        if href and href != plain_irc(body).strip():
            body += " (" + href + ")"
    if name == "blockquote":
        return (
            "\n"
            + "\n".join("> " + line for line in body.strip(" \t\r\n").split("\n"))
            + "\n"
        )
    if name == "cite":
        return body.strip() + ":\n"
    if name == "li":
        marker = "- "
        if node.parent.name == "ol":
            marker = (
                str(list(node.parent.find_all("li", recursive=False)).index(node) + 1)
                + ". "
            )
        body = marker + body.strip(" \t\r\n") + "\n"
    if styled:
        if color and color != inherited_color:
            end = (
                ("\x04" + inherited_color)
                if inherited_color
                else ("\x0f" + "".join(sorted(active | local)))
            )
            body = "\n".join(
                "\x04" + color + line + end if line else "" for line in body.split("\n")
            )
        if "spoiler" in node.get("class", []) or "spoiler" in name:
            end = (
                "\x0f"
                + "".join(sorted(active | local))
                + ("\x04" + inherited_color if inherited_color else "")
            )
            body = "\n".join(
                "\x0301,01" + line + end if line else "" for line in body.split("\n")
            )
        if toggle:
            body = "\n".join(
                toggle + line + toggle[::-1] if line else ""
                for line in body.split("\n")
            )
    if name in ("p", "div", "ul", "ol", "pre", "h1", "h2", "h3", "tr"):
        body += "\n"
    if name in ("td", "th"):
        body += " | "
    return body


def html_to_irc(raw, base_url, styled=True):
    body = render_html(soup_for(raw), base_url, styled)
    return "\n".join(
        line.strip(" \t\r") for line in body.split("\n") if plain_irc(line).strip()
    )


@dataclass
class SiteReply:
    author: str
    quoted: str
    body: str


def extract_reply(raw, base_url):
    soup = soup_for(raw)
    quote = soup.find("blockquote")
    if quote is None or quote.parent is not soup:
        return None
    # Só interpretar uma citação no início, com autor explícito. Previews de
    # links também usam blockquote no site, mas não têm cite.
    if any(str(x).strip() for x in quote.previous_siblings):
        return None
    cite = quote.find("cite", recursive=False)
    if cite is None:
        return None
    author = cite.get_text().strip().lstrip("@")
    cite.decompose()
    for nested in quote.find_all("blockquote"):
        nested.decompose()
    quoted = html_to_irc(str(quote), base_url, styled=False)
    quoted = "\n".join(line.removeprefix("> ").strip() for line in quoted.splitlines())
    quote.decompose()
    return SiteReply(author, quoted, html_to_irc(str(soup), base_url))


def reply_context(reply_to):
    """Linha de contexto de um reply nativo cujo original o cliente não tem."""
    user = reply_to.get("user") or {}
    author = user.get("username") or "?"
    excerpt = " ".join(str(reply_to.get("excerpt") or "").split())
    return f"> {author}: {excerpt or 'Imagem ou mídia'}"


def normalized_quote(text):
    return " ".join(
        html.unescape(plain_irc(text)).replace("［", "[").replace("］", "]").split()
    )
