# CapybaraBR → IRC local

Bridge Python entre a sala `General` do CapybaraBR e o canal IRC `#capybara`.
O Halloy conecta a um servidor local em `127.0.0.1:6667`. As mensagens enviadas
ao canal são publicadas com a conta autenticada pelos cookies.
O nickname IRC não muda a identidade da conta no site.

## Iniciar no Windows

Python 3.11 ou superior. No PowerShell, dentro desta pasta:

```powershell
.\start.ps1
```

O script cria `.venv` e instala `requirements.txt` no primeiro uso. Mantenha
o terminal aberto; `Ctrl+C` encerra o servidor. Se a política do PowerShell
impedir executar o script, use diretamente:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python irc_bridge.py
```

## Executar com Docker ou Podman

A imagem é publicada no GitHub Container Registry pelo workflow do repositório.
O bridge aceita `--bind 0.0.0.0` para atender outros containers; no uso local,
o endereço padrão continua sendo `127.0.0.1`.

Para usar junto com o Lurker, copie `compose.lurker.yaml` para a pasta do
Compose do Lurker. O arquivo espera encontrar os cookies em
`../capybara-irc-bridge/capybarabr.txt`; essa localização pode ser alterada
pela variável `CAPYBARA_COOKIES`. O arquivo de cookies não faz parte da imagem
nem do repositório.

No Lurker, configure o servidor IRC como `capybara-bridge`, porta `6667`, sem
TLS. Os dois serviços compartilham a rede padrão do Compose.

Como o repositório e a imagem são privados, autentique o Podman no GHCR no
servidor antes de iniciar o Compose. Quando o Podman solicitar a senha, informe
um token do GitHub com permissão de leitura de pacotes (`read:packages`):

```sh
podman login ghcr.io -u SEU_USUARIO_GITHUB
```

Depois, na pasta do Compose do Lurker:

```sh
podman-compose up -d
```

## Halloy

Adicione o bloco de `halloy.toml` ao seu `config.toml`, preservando as demais
configurações. Os campos seguem a [documentação do Halloy](https://halloy.chat/configuration/servers).

```toml
[servers.CapybaraBR]
server = "127.0.0.1"
port = 6667
use_tls = false
nickname = "seu_user_igual_do_site"
channels = ["#capybara"]
```

Inicie o bridge antes de conectar o Halloy. O canal mostra as últimas 20
mensagens e os usuários presentes. Novas mensagens chegam via WebSocket;
o histórico também é consultado a cada 30 segundos para recuperar mensagens
perdidas. A deduplicação usa o ID de cada mensagem. Os horários do site são
enviados com `server-time` quando o cliente negocia essa capacidade.

Mensagens normais e `/me` no canal são enviadas ao site. Links e imagens
recebidos aparecem como URLs; figurinhas aparecem como imagem (veja abaixo).
Mensagens diretas do site viram queries IRC (veja abaixo). Upload de
arquivos e comandos de moderação não estão implementados. Remoções no site geram um aviso, pois IRC comum
não apaga mensagens do histórico do cliente.

## Cores dos nicknames

O bridge publica a cor de `user.group.color` por IRCv3 `draft/metadata-2`,
na chave `color`. A cor do status online/ausente não é usada como cor do nome.
As cores são enviadas para usuários no histórico e na lista de presença, e
atualizadas quando mensagens, presença ou a reconciliação HTTP trazem mudanças
de grupo. Valores inválidos são descartados. Metadata do site é somente leitura.

Use Halloy 2026.7 ou superior com suporte a metadata. Mescle o bloco abaixo
com `[display]` no seu `config.toml` (também incluído em `halloy.toml`):

```toml
[display]
nickname = ["color"]
nicklist_nickname = ["color"]
adapt_metadata_colors = "none"
```

Se já usa `"display-name"` ou `"pronouns"` nessas listas, mantenha-os e adicione
`"color"`. `"none"` mantém o RGB exato do site; `"illegible"` permite ao Halloy
ajustar somente cores com contraste ruim. Esta é uma configuração global do
Halloy, conforme a [documentação de display](https://halloy.chat/configuration/display).
Reinicie o bridge e reconecte o cliente para negociar metadata e batch.

## Formatação e replies

O bridge converte a formatação IRC em BBCode no envio e o HTML do site em
formatação IRC na leitura. No Halloy, use `/format` (ou `/f`) para formatar
texto, conforme a [documentação do cliente](https://halloy.chat/guides/text-formatting).
Também é possível escrever BBCode diretamente.

| Conteúdo                              | IRC → site                                                  | Site → IRC                                               |
| ------------------------------------- | ----------------------------------------------------------- | -------------------------------------------------------- |
| Negrito, itálico, sublinhado, tachado | `[b]`, `[i]`, `[u]`, `[s]`                                  | Estilos nativos IRC                                      |
| Código/monoespaçado                   | `[code]`                                                    | Monoespaçado IRC                                         |
| Cores                                 | 16 cores básicas e RGB → `[color=#RRGGBB]`                  | Cores CSS reconhecidas → RGB IRC                         |
| Spoiler                               | Texto com foreground/background iguais → `[spoiler]`        | Elemento spoiler → cores iguais no IRC                   |
| URL direta de imagem                  | Envolvida automaticamente em `[img]…[/img]`                 | URL da imagem                                            |
| Vídeo YouTube | BBCode `[youtube]ID[/youtube]` preservado no envio | Link completo `https://www.youtube.com/watch?v=ID`, inclusive para embeds HTML |
| Links com título                      | BBCode `[url=…]…[/url]` pode ser escrito diretamente        | Título e URL                                             |
| Reply do Halloy                       | Reply nativo do site (`reply_to_id`), sem `[quote]`         | `+reply`/`+draft/reply` com o ID do original             |
| Reply cujo original não está no IRC   | —                                                           | Linha `> usuario: trecho` antes da resposta              |
| Citações `[quote]` antigas            | BBCode preservado                                           | `+reply` se o original é identificado; senão `> usuario:` |
| Listas, parágrafos, quebras           | BBCode manual preservado                                    | Marcadores e linhas de texto                             |

Exemplos de envio:

```text
Veja https://exemplo.com/capa.jpg
→ Veja [img]https://exemplo.com/capa.jpg[/img]

/format __negrito__ _itálico_ ~~tachado~~ `código`
→ [b]negrito[/b] [i]itálico[/i] [s]tachado[/s] [code]código[/code]
```

URLs com extensão de imagem, query string ou URL de imagem dentro de um
proxy `?url=…` são reconhecidas sem fazer requisições a terceiros. Para uma
URL sem extensão identificável, escreva `[img]URL[/img]`. URLs dentro de
`[img]`, `[url]`, `[code]` e `[quote]` existentes não são envolvidas novamente.

Para responder, selecione a mensagem original na ação de reply do Halloy.
O bridge envia a resposta como reply nativo do chat (o mesmo botão
"Responder" do site), e remove o prefixo `nick: ` que o Halloy acrescenta.
Só aceita IDs recebidos nesta conexão e ainda disponíveis no cache. Como no
site, não é possível responder a mensagens de bots ou da conta de sistema.

Replies feitos no navegador trazem o ID do original, autor e um trecho do
texto. Quando o original foi entregue nesta conexão, o bridge cria a
referência IRCv3; caso contrário, mostra a linha `> usuario: trecho` antes
da resposta. Mensagens antigas, anteriores ao reply nativo, ainda usam a
citação no texto: a referência só é criada se existir uma única mensagem
anterior desse autor com o mesmo conteúdo no cache. Reconecte o Halloy para
negociar `message-tags`.

Fontes, tamanhos, alinhamento, tabelas e backgrounds não têm equivalência
completa em IRC: o conteúdo é preservado como texto; background só é convertido
para spoiler quando as cores de frente e fundo são iguais. O efeito visual
do spoiler depende do cliente. Os estilos são reabertos em mensagens longas
e encerrados em cada fragmento para não vazar para mensagens seguintes.

## Mensagem fixada

A mensagem fixada da sala vira o tópico do `#capybara`, por exemplo
`CapybaraBR / General | 📌 LoboGuara: trecho da mensagem`, com quem fixou e
quando. Ao fixar, desafixar ou apagar a fixada no site, o tópico muda na hora
(o cliente mostra a troca como uma mudança de tópico); fixações feitas com o
bridge desconectado do site aparecem ao reconectar. Só moderadores fixam, e
apenas pelo site: `/topic` pelo IRC responde com erro.

## Mensagens diretas

As conversas privadas do chat (a lista "ali do lado" no site) aparecem como
queries IRC: uma DM de `fulano` abre a conversa com `fulano` no cliente. Para
responder ou iniciar uma conversa, use a query normalmente ou `/msg fulano texto`.
A mensagem é enviada pela conta do site, como DM.

- Chegam em tempo real pelo canal privado do usuário no WebSocket, e também
  na atualização HTTP a cada 30 segundos.
- DMs enviadas pela sua conta no navegador aparecem na query do destinatário,
  como mensagens suas (com `echo-message`).
- Reply nativo funciona nas DMs: selecione a mensagem na ação de reply do Halloy.
  Figurinhas também: cole a URL da figurinha na query.
- O destinatário precisa ser conhecido pelo bridge: estar online no chat, ter
  conversa aberta no site ou ter aparecido no histórico. Caso contrário, o
  servidor responde `401`.
- DMs recebidas sem nenhum cliente conectado ficam no cache do bridge e são
  entregues, com o horário original, na próxima conexão. Após reiniciar o
  bridge, as DMs ainda disponíveis no site são entregues na primeira conexão.
- Reações e indicador de digitação ainda não são repassados nas DMs.

## Figurinhas

As imagens das figurinhas exigem login no site, então o bridge as entrega
por um servidor HTTP local, usando os cookies: `http://127.0.0.1:6668/stickers/ID`
(a porta é `--port` + 1, ou `--http-port`). Uma figurinha recebida aparece como:

```text
[figurinha: Fig1] http://127.0.0.1:6668/stickers/20
```

Para enviar, nos dois clientes:

```text
/fig                → lista as figurinhas disponíveis, com número e pacote
/fig 20             → envia a figurinha 20
/fig bom dia        → envia pelo nome (sem diferenciar maiúsculas e acentos)
```

Para dar nomes próprios às figurinhas, edite `figurinhas.json` (ou indique
outro arquivo com `--sticker-names`) e reinicie o bridge. Os nomes valem só
no IRC: aparecem na lista e nas figurinhas recebidas, e o `/fig` aceita tanto
o nome local quanto o original do site.

```json
{
  "2": "Aperto de mão",
  "3": "Almoço"
}
```

Se o nome corresponder a mais de uma figurinha, o bridge lista as opções e
não envia nada. Uma mensagem que contém somente a URL de uma figurinha
(a local ou a do site) também é enviada como figurinha. Assim, no Halloy,
é possível responder com figurinha colando a URL na ação de reply.

**Halloy** detecta imagens pelo conteúdo e mostra o preview com a configuração
padrão (`[preview] enabled = true`).

**The Lounge** só mostra previews com `prefetch: true` no `config.js`. Use também
`prefetchStorage: true`: o servidor do The Lounge baixa a imagem e a entrega
ao navegador, então funciona mesmo abrindo o The Lounge de outro dispositivo.
Em ambos os casos, o servidor do The Lounge precisa rodar na mesma máquina do
bridge, porque a URL aponta para `127.0.0.1`. Com `prefetchStorage: false`,
quem busca a imagem é o navegador, que também precisa estar nesta máquina.
O The Lounge não envia replies IRCv3; nele, `/fig` envia sem reply.

## Reações no Halloy

Halloy 2026.4 ou superior suporta [reações IRCv3](https://ircv3.net/specs/client-tags/react).
Reinicie o bridge e reconecte o Halloy após atualizar o código, para negociar
`message-tags`. Use a ação de reação da mensagem no Halloy para adicionar
um emoji; a ação de remover sua reação também é repassada ao site.

As reações existentes acompanham o histórico, e alterações chegam via
WebSocket ou na atualização HTTP. Cada mensagem recebe um `msgid` estável
que corresponde ao ID no site. Reações usam `TAGMSG`, `+reply`/`+draft/reply`,
`+draft/react` e `+draft/unreact`. Em mensagens divididas em vários fragmentos
IRC, a reação fica vinculada ao primeiro fragmento. Só é possível reagir
a mensagens recebidas nesta conexão e ainda disponíveis no cache de 100
mensagens do bridge. Clientes sem `message-tags` recebem avisos textuais.

O site usa uma API que alterna a reação. O bridge consulta o estado antes
do envio para transformar adicionar/remover em ações explícitas. Se a mesma
conta alterar a mesma reação simultaneamente pelo navegador, existe uma
janela de concorrência entre a consulta e o POST; a API não oferece operações
separadas para adicionar e remover.

## Indicador de digitação

O bridge traduz os eventos de digitação do site para a tag
[`+typing` do IRCv3](https://ircv3.net/specs/client-tags/typing). No sentido
contrário, a digitação no IRC é enviada ao site como a conta autenticada.
Eventos são limitados a um a cada três segundos por usuário/sala.

O exemplo `halloy.toml` já habilita exibição e compartilhamento. Na sua
configuração, adicione o bloco abaixo, conforme a
[documentação do Halloy](https://halloy.chat/configuration/servers#typing):

```toml
[servers.CapybaraBR.typing]
show = true
share = true
```

Reinicie o bridge e reconecte o Halloy para negociar `message-tags` e receber
a atualização de `CLIENTTAGDENY`. Os indicadores são eventos temporários;
não aparecem como mensagens no histórico. O site só transmite pulsos de
digitação, sem evento de parada: o IRC expira o indicador após seis segundos
sem novos pulsos, e o site usa seu próprio prazo de quinze segundos.

## Cookies e opções

O arquivo `capybarabr.txt` deve estar no formato Netscape exportado
do navegador com a conta logada. Ele contém credenciais de sessão; está
excluído pelo `.gitignore`. O bridge mantém cookies renovados em memória,
sem sobrescrever o arquivo original. Quando a sessão deixar de funcionar,
exporte cookies novos, substitua o arquivo e reinicie o bridge.

```powershell
# Testar autenticação, histórico e WebSocket sem publicar mensagens:
.\.venv\Scripts\python irc_bridge.py --check

# Outra porta (figurinhas em 6669), histórico de 50 mensagens:
.\start.ps1 --port 6668 --history 50

# Sala System, em vez de General; o canal IRC continua #capybara:
.\start.ps1 --room 2

# Arquivo de cookies diferente:
.\start.ps1 --cookies "C:\caminho\cookies.txt"
```

Os servidores IRC e HTTP escutam somente em `127.0.0.1`. Ajuste a porta no Halloy se usar
`--port`. O cliente Socket.IO e o Engine.IO estão fixados nas versões
compatíveis com o protocolo Engine.IO 3 observado no site.

## Verificação

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Os testes exercitam conexões TCP IRC reais com uma API simulada: negociação
CAP, registro, entrada no canal, histórico, envio, presença, deduplicação,
reações, remoção de reações, digitação, tags escapadas, HTML e limite de 512 bytes em UTF-8.
`--check` verifica o site real. O envio
público é validado com API simulada; nenhuma mensagem de teste foi publicada
na conta do site.
