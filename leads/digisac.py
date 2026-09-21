#!/usr/bin/env python3
r"""
Cliente da API do DigiSac.

Nao e a Cloud API da Meta: o DigiSac e o intermediario, e e ele quem fala com
o WhatsApp. Uma mensagem e um POST /messages.

Sem dependencia nova: urllib da biblioteca padrao. O volume aqui e uma
requisicao a cada 3-6 segundos -- nada que justifique trazer requests para o
projeto so por isso.

Credenciais saem de variavel de ambiente (leads/config.py). Nao existe
caminho neste arquivo que escreva o token em log ou em mensagem de erro.
"""
import json
import secrets
import urllib.error
import urllib.request

from . import config

TEMPO_LIMITE = 30          # segundos por requisicao


class ErroDigiSac(Exception):
    """Falha de envio. A mensagem e a que o DigiSac devolveu, quando devolve
    alguma -- e o que permite distinguir "numero nao tem WhatsApp" de "token
    invalido" sem abrir o painel deles."""

    def __init__(self, mensagem, codigo=None, definitivo=False):
        super().__init__(mensagem)
        self.codigo = codigo
        # definitivo = nao adianta tentar de novo (numero nao existe, token
        # errado). O contrario e falha de rede, que merece nova tentativa.
        self.definitivo = definitivo


def configurado():
    return bool(config.DIGISAC_SUBDOMINIO and config.DIGISAC_TOKEN
                and config.DIGISAC_SERVICE_ID)


def _base():
    return f"https://{config.DIGISAC_SUBDOMINIO}.digisac.io/api/v1"


# O DigiSac fica atras do Cloudflare, e o Cloudflare recusa a assinatura
# padrao do urllib com "error code: 1010" -- banimento por assinatura de
# navegador. A requisicao nem chega no DigiSac: token certo dava o mesmo erro,
# e a mensagem manda procurar no lugar errado.
#
# Um User-Agent de cliente de verdade resolve. Nao e disfarce para burlar
# limite: e identificar-se como um cliente HTTP comum em vez de ficar com o
# rotulo generico da biblioteca, que a protecao anti-bot trata como robo de
# varredura.
CABECALHO_AGENTE = "MaquinaDeLeads/1.0 (+https://zebrahads.com.br)"


def _chamar(caminho, corpo=None, metodo="POST"):
    dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(
        _base() + caminho, data=dados, method=metodo,
        headers={
            "Authorization": f"Bearer {config.DIGISAC_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": CABECALHO_AGENTE,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TEMPO_LIMITE) as r:
            texto = r.read().decode("utf-8", "replace")
            return json.loads(texto) if texto.strip() else {}
    except urllib.error.HTTPError as e:
        bruto = e.read().decode("utf-8", "replace")
        try:
            detalhe = json.loads(bruto)
            msg = (detalhe.get("message") or detalhe.get("error")
                   or bruto[:300])
        except ValueError:
            msg = bruto[:300] or f"HTTP {e.code}"

        # 1010 e do Cloudflare, nao do DigiSac: a requisicao foi barrada
        # antes de chegar la. Sem dizer isso, o erro manda conferir token.
        if "1010" in str(msg):
            msg = ("Bloqueado pelo Cloudflare na frente do DigiSac "
                   "(error code: 1010) -- a requisicao nao chegou na API "
                   "deles. Nao e problema de token.")
        # 4xx e erro nosso (numero, token, payload): repetir da o mesmo.
        # 5xx e do lado deles e costuma passar.
        #
        # Excecao: queda de conexao chega como 4xx e NAO e definitiva. O
        # destino tem de voltar para a fila -- o problema e o chip, nao ele.
        definitivo = 400 <= e.code < 500 and not e_queda_de_conexao(msg)
        raise ErroDigiSac(str(msg), codigo=e.code, definitivo=definitivo)
    except urllib.error.URLError as e:
        raise ErroDigiSac(f"Sem resposta do DigiSac: {e.reason}", definitivo=False)
    except json.JSONDecodeError:
        raise ErroDigiSac("Resposta do DigiSac nao era JSON", definitivo=False)


# Falhas que sao da CONEXAO, e nao do numero de destino.
#
# "Service (...) disconnected" significa que o chip caiu do WhatsApp Web e o
# DigiSac esta pedindo o QR de novo. Queimar o destino nisso e injusto com o
# lead e com a lista: ele nunca foi tentado de verdade.
SINAIS_DE_QUEDA = ("disconnected", "not connected", "desconectado",
                   "qrcode", "qr code", "session closed", "unauthorized device")


def e_queda_de_conexao(mensagem):
    m = str(mensagem or "").lower()
    return any(s in m for s in SINAIS_DE_QUEDA)


def estado_conexao():
    """(conectado, detalhe) da conexao de WhatsApp que envia.

    Existe porque o POST /messages NAO falha quando o chip esta fora: o
    DigiSac aceita a mensagem e a deixa na fila dele, com o relogio ao lado
    na tela de conversas. Do nosso lado parece envio bem-sucedido, e uma
    campanha inteira pode ser despejada numa fila que nao anda.

    conectado None = nao deu para saber (caminho desconhecido na API). Nesse
    caso quem chama deve seguir, e nao bloquear: melhor arriscar o envio do
    que travar o disparo por causa de um endpoint que nao achamos.
    """
    if not configurado():
        return False, "DigiSac nao configurado"

    ident = config.DIGISAC_SERVICE_ID
    for caminho in (f"/connections/{ident}", f"/services/{ident}"):
        try:
            r = _chamar(caminho, metodo="GET")
        except ErroDigiSac:
            continue
        if not isinstance(r, dict):
            continue
        dados = r.get("data") if isinstance(r.get("data"), dict) else r
        if _caiu_a_conexao(dados):
            return False, "o numero esta desconectado do WhatsApp"
        conectado = _cavar(dados, ("isconnected", "isConnected"))
        if conectado is True:
            return True, "numero conectado"
        estado = _cavar(dados, ("state", "status"))
        if isinstance(estado, str) and estado:
            baixo = estado.lower()
            if baixo in ("connected", "normal", "online", "open"):
                return True, f"numero {baixo}"
            return False, f"conexao em estado '{estado}'"
    return None, "nao foi possivel ler o estado da conexao"


def testar():
    """(ok, mensagem) para a tela mostrar ANTES de uma campanha de horas.

    Testa as DUAS coisas. So o token respondia antes, e isso dizia
    "Conectado" com o chip fora do WhatsApp -- a pior resposta possivel,
    porque da confianca para iniciar uma campanha que vai so encher a fila
    do DigiSac.
    """
    if not configurado():
        return False, "Faltam DIGISAC_SUBDOMINIO, DIGISAC_TOKEN ou DIGISAC_SERVICE_ID no .env"
    try:
        _chamar("/contacts?perPage=1", metodo="GET")
    except ErroDigiSac as e:
        return False, str(e)

    conectado, detalhe = estado_conexao()
    if conectado is False:
        return False, (f"Token e subdominio certos, mas {detalhe}. "
                       f"Reconecte o numero no painel do DigiSac (vai pedir o "
                       f"QR) -- sem isso a mensagem entra na fila deles e nao "
                       f"sai.")
    if conectado is None:
        return True, ("Token e subdominio certos. Nao consegui conferir se o "
                      "numero esta conectado ao WhatsApp -- confira no painel "
                      "do DigiSac antes de uma campanha grande.")
    return True, "Conectado, e o numero esta online no WhatsApp"


# Onde a lista de conexoes pode estar. O produto chama de "conexao" na tela
# e de "service" no corpo da mensagem, e a doc nao esta acessivel sem login,
# entao tentamos os nomes plausiveis em ordem e ficamos com o primeiro que
# responder. Descobrir isso na tela custa um clique; adivinhar custa uma
# campanha inteira apontada para o numero errado.
CAMINHOS_CONEXOES = ("/connections", "/services", "/connection", "/service")


def listar_conexoes():
    """[(id, nome, detalhe)] das conexoes da conta, ou levanta ErroDigiSac.

    Serve para o painel mostrar de onde sai o disparo em vez de a pessoa ter
    que garimpar o id na URL do painel deles.
    """
    if not (config.DIGISAC_SUBDOMINIO and config.DIGISAC_TOKEN):
        raise ErroDigiSac("Preencha o subdominio e o token primeiro.",
                          definitivo=True)

    ultimo = None
    for caminho in CAMINHOS_CONEXOES:
        try:
            r = _chamar(caminho + "?perPage=100", metodo="GET")
        except ErroDigiSac as e:
            ultimo = e
            if e.codigo in (401, 403):
                raise                 # token ruim: trocar de caminho nao ajuda
            continue

        itens = r if isinstance(r, list) else (
            r.get("data") or r.get("items") or r.get("results") or [])
        if not isinstance(itens, list):
            continue

        saida = []
        for it in itens:
            if not isinstance(it, dict):
                continue
            ident = it.get("id") or it.get("serviceId") or ""
            if not ident:
                continue
            nome = (it.get("name") or it.get("label") or it.get("title")
                    or it.get("description") or "(sem nome)")
            partes = [str(it[k]) for k in ("type", "number", "phone", "status")
                      if it.get(k)]
            saida.append((str(ident), str(nome), " · ".join(partes)))
        if saida:
            return saida

    raise ErroDigiSac(
        "Nao consegui listar as conexoes. " +
        (f"Ultima resposta: {ultimo}" if ultimo else
         "Nenhum dos caminhos conhecidos respondeu com uma lista."),
        definitivo=True)


def conferir_service_id():
    """(ok, detalhe) -- o Service ID configurado e a conexao que esta online?

    Existe por causa de uma falha real: reconectar o numero no painel do
    DigiSac pode criar uma conexao NOVA, com id novo. O .env continua
    apontando para a antiga, que ainda existe e ainda aceita POST /messages
    -- e devolve 200 com id de mensagem. Mas ela esta orfa, e a mensagem
    nunca e despachada: fica em ack 0 para sempre.

    Visto de fora isso e indistinguivel de sucesso, e o painel deles mostra
    "Conectado" porque esta falando da conexao nova. Por isso a conferencia
    compara o id configurado com a LISTA, em vez de so perguntar o estado
    do id configurado.

    ok None = nao deu para listar. Nesse caso nao se afirma nada.
    """
    if not configurado():
        return False, "DigiSac nao configurado"

    try:
        conexoes = listar_conexoes()
    except ErroDigiSac as e:
        return None, f"nao consegui listar as conexoes ({e})"

    atual = config.DIGISAC_SERVICE_ID
    ids = [c[0] for c in conexoes]
    if atual not in ids:
        nomes = ", ".join(f"{c[1]} ({c[0]})" for c in conexoes[:4])
        return False, (
            f"o Service ID do .env ({atual}) NAO esta entre as conexoes da "
            f"conta. Isso acontece quando a conexao e recriada ao reconectar: "
            f"a antiga fica orfa, aceita a mensagem e nunca a entrega. "
            f"Conexoes existentes: {nomes or '(nenhuma)'}")

    # Esta na lista: confere se e a que esta de fato conectada.
    conectado, detalhe = estado_conexao()
    if conectado is False:
        return False, f"o Service ID esta correto, mas {detalhe}"
    return True, "o Service ID aponta para uma conexao existente"


def enviar(numero, texto, arquivo=None):
    """Manda uma mensagem. Devolve o id dela no DigiSac.

    dontOpenTicket=True de proposito: sem isso cada disparo abre um
    atendimento, e uma campanha de 5 mil numeros despeja 5 mil chamados na
    caixa de quem atende de verdade.
    """
    if not configurado():
        raise ErroDigiSac("DigiSac nao configurado no .env", definitivo=True)

    corpo = {
        "serviceId": config.DIGISAC_SERVICE_ID,
        "number": numero,
        "text": texto,
        "origin": "bot",
        "dontOpenTicket": True,
    }
    if arquivo:
        corpo["file"] = arquivo          # {base64, mimetype, name}

    r = _chamar("/messages", corpo)
    return r.get("id") or (r.get("data") or {}).get("id") or ""


def ver_mensagem(msg_id):
    """Pergunta ao DigiSac o que ele fez com uma mensagem que ele aceitou.

    Existe porque "aceitou" e "entregou" sao coisas diferentes no DigiSac: o
    POST /messages devolve 200 e um id, e a mensagem pode ficar parada na
    fila dele sem nunca ir para o WhatsApp. Do nosso lado isso e
    indistinguivel de sucesso.

    Este e o unico jeito de saber de que lado esta o problema sem abrir
    ticket no suporte deles: se ele responde que a mensagem esta pendente ou
    com erro, o envio saiu daqui e travou la.
    """
    if not configurado():
        raise ErroDigiSac("DigiSac nao configurado", definitivo=True)
    if not msg_id:
        raise ErroDigiSac("Sem id de mensagem para consultar", definitivo=True)

    ultimo = None
    for caminho in (f"/messages/{msg_id}", f"/message/{msg_id}"):
        try:
            r = _chamar(caminho, metodo="GET")
        except ErroDigiSac as e:
            ultimo = e
            continue
        if isinstance(r, dict):
            return r.get("data") if isinstance(r.get("data"), dict) else r
    raise ErroDigiSac(
        f"Nao consegui consultar a mensagem. {ultimo or ''}".strip(),
        definitivo=True)


# Niveis de confirmacao do WhatsApp, como o DigiSac devolve em "ack".
#
#   -1  erro
#    0  pendente -- aceita pelo DigiSac e NAO despachada
#    1  chegou no servidor do WhatsApp (um tique)
#    2  entregue no aparelho (dois tiques)
#    3  lida (dois tiques azuis)
ACK_ERRO, ACK_PENDENTE, ACK_SERVIDOR = -1, 0, 1


def ack_da_mensagem(msg_id):
    """ack de uma mensagem, ou None se nao der para saber.

    E o unico jeito de distinguir "o DigiSac aceitou" de "o WhatsApp
    recebeu". O POST /messages devolve 200 nos dois casos.
    """
    try:
        dados = ver_mensagem(msg_id)
    except ErroDigiSac:
        return None
    bruto = _cavar(dados, ("ack",)) if isinstance(dados, dict) else None
    if bruto is None:
        return None
    try:
        return int(bruto)
    except (TypeError, ValueError):
        return None


def resumir_mensagem(dados):
    """Reduz a resposta de ver_mensagem ao que decide o diagnostico.

    Os nomes dos campos vem dos tipos do SDK oficial da fabricante
    (@ikatec/digisac-api-sdk), e nao de chute:

      whatsappMessageId  ausente = a mensagem NUNCA chegou ao WhatsApp.
                         Este e o campo decisivo: ficou so no banco deles.
      sent               o proprio DigiSac dizendo se despachou.
      ack                -1 erro, 0 pendente, 1 servidor, 2 entregue, 3 lida.
      startBlockedAt     regra de bloqueio de mensagem ativa retendo o envio
      unblockUntilAt     ate quando fica retida
    """
    if not isinstance(dados, dict):
        return {"bruto": str(dados)[:2000]}
    wamid = _cavar(dados, ("whatsappMessageId", "whatsapp_message_id"),
                   so_texto=True)
    return {
        "id": _cavar(dados, ("id",)) or "",
        "status": _cavar(dados, ("ack", "status", "messageStatus", "state")),
        # None quando o campo nao vem; a tela distingue isso de False.
        "despachada": _cavar(dados, ("sent",)),
        "wamid": wamid or "",
        "retida_em": _cavar(dados, ("startBlockedAt", "start_blocked_at"),
                            so_texto=True),
        "retida_ate": _cavar(dados, ("unblockUntilAt", "unblock_until_at"),
                             so_texto=True),
        "erro": _cavar(dados, ("errorDescription", "error", "errorMessage",
                               "failReason", "statusMessage"), so_texto=True),
        "bruto": json.dumps(dados, ensure_ascii=False)[:2000],
    }


# Flags que de fato indicam que a camada de despacho esta viva.
#
# "Conectado (verde)" no painel deles le apenas isConnected, que e UM flag
# entre cerca de 25 em Service.data.status. A sessao pode estar
# semi-conectada: isConnected verdadeiro e a camada que envia morta. Nesse
# estado a API aceita, cria a conversa, devolve 200 -- e o ack nunca chega.
FLAGS_DESPACHO = ("isConnected", "isWebConnected", "isPhoneConnected",
                  "isPhoneAuthed", "isLidMigrated", "isConflicted",
                  "isWaitingForPhoneInternet", "isOnQrPage", "isSyncing",
                  "isWebSyncing", "mode", "state", "disconnectedAt")


def diagnostico_conexao():
    """Os flags reais da conexao, e nao so o verde do painel.

    Devolve (problemas, flags). problemas e a lista de sinais ruins em
    linguagem de gente; flags e o que veio, para leitura humana.
    """
    if not configurado():
        return ["DigiSac nao configurado"], {}

    ident = config.DIGISAC_SERVICE_ID
    bruto = None
    for caminho in (f"/services/{ident}", f"/connections/{ident}"):
        try:
            r = _chamar(caminho, metodo="GET")
        except ErroDigiSac:
            continue
        if isinstance(r, dict):
            bruto = r.get("data") if isinstance(r.get("data"), dict) else r
            break
    if bruto is None:
        return ["nao consegui ler o estado da conexao"], {}

    status = bruto.get("status") if isinstance(bruto.get("status"), dict) else bruto
    if not isinstance(status, dict):
        status = bruto
    flags = {}
    for f in FLAGS_DESPACHO:
        v = _cavar(status, (f, f[0].lower() + f[1:], f.lower()))
        if v is not None:
            flags[f] = v

    ruins = []
    def falso(nome):
        return flags.get(nome) is False

    if falso("isConnected"):
        ruins.append("a conexao esta desconectada")
    if falso("isWebConnected"):
        ruins.append("a camada web nao esta conectada -- e ela que despacha")
    if falso("isPhoneConnected"):
        ruins.append("o celular nao esta alcancavel")
    if falso("isPhoneAuthed"):
        ruins.append("o celular nao esta autenticado")
    if flags.get("isConflicted") is True:
        ruins.append("a sessao esta em conflito (o WhatsApp foi aberto em "
                     "outro lugar)")
    if flags.get("isWaitingForPhoneInternet") is True:
        ruins.append("esperando internet no celular")
    if flags.get("isOnQrPage") is True:
        ruins.append("esta na tela de QR -- o pareamento nao terminou")
    if falso("isLidMigrated"):
        ruins.append("a migracao de identificador (LID) do WhatsApp nao foi "
                     "concluida; em outras plataformas isso faz exatamente "
                     "a mensagem sair da API e nunca receber confirmacao")

    # settings tambem interessam: o DigiSac pode estar retendo envio ativo.
    ajustes = bruto.get("settings") if isinstance(bruto.get("settings"), dict) else {}
    for nome, recado in (
            ("blockMessageRulesActive",
             "as regras de bloqueio de mensagem estao ativas -- elas retem "
             "envio para quem nunca respondeu"),
            ("unblockByReceiveMessage",
             "o envio so e liberado depois que o contato responder")):
        if ajustes.get(nome) is True:
            ruins.append(recado)
            flags[nome] = True

    return ruins, flags


def reiniciar_conexao():
    """POST /services/<id>/restart -- reinicia a sessao sem pedir QR novo.

    E a tentativa mais barata contra sessao semi-conectada: o pareamento
    continua valendo, so a camada de envio sobe de novo. Se nao resolver,
    o caminho seguinte e logout + refazer o pareamento inteiro, que exige
    celular na mao.
    """
    if not configurado():
        raise ErroDigiSac("DigiSac nao configurado", definitivo=True)
    ident = config.DIGISAC_SERVICE_ID
    ultimo = None
    for caminho in (f"/services/{ident}/restart",
                    f"/connections/{ident}/restart"):
        try:
            _chamar(caminho, corpo={})
            return True
        except ErroDigiSac as e:
            ultimo = e
    raise ultimo or ErroDigiSac("nao consegui reiniciar", definitivo=True)


# ------------------------------------------------------------ webhook


def segredo_webhook():
    """Palavra aleatoria no fim da URL do webhook, guardada em disco.

    O webhook e rota publica -- o DigiSac nao faz login. Sem nada que
    identifique quem chama, o endereco e adivinhavel e qualquer um posta
    status de entrega falso ou finge ser um lead pedindo descadastro.

    Fica em arquivo pelo mesmo motivo do segredo do cookie: gerar a cada
    partida invalidaria a URL ja salva no painel do DigiSac a cada reinicio
    do servico.
    """
    caminho = config.DIR_DADOS / "webhook.txt"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    if caminho.exists():
        valor = caminho.read_text(encoding="utf-8").strip()
        if valor:
            return valor
    valor = secrets.token_urlsafe(24)
    caminho.write_text(valor, encoding="utf-8")
    try:
        caminho.chmod(0o600)
    except OSError:
        pass                     # Windows ignora; o arquivo segue fora do Git
    return valor


def url_webhook():
    return f"{config.SITE_URL}/webhook/digisac/{segredo_webhook()}"


def _cavar(d, chaves, profundidade=4, so_texto=False):
    """Procura a primeira das chaves em qualquer nivel do dicionario.

    O payload do DigiSac nao esta publicado em lugar que de para ler sem
    login, e o mesmo dado aparece em nivel diferente conforme o evento --
    as vezes na raiz, as vezes em data, as vezes em data.message. Procurar
    em largura cobre os tres sem ter que acertar o caminho de primeira.
    """
    if not isinstance(d, dict) or profundidade < 0:
        return None
    for k in chaves:
        v = d.get(k)
        if v in (None, "", {}, []):
            continue
        # so_texto evita o engano de "message": em alguns eventos e o texto
        # da mensagem, em outros e o objeto inteiro dela. Sem o filtro, o
        # dicionario virava texto e o pedido de parada nunca seria lido.
        if so_texto and not isinstance(v, (str, int, float)):
            continue
        return v
    for v in d.values():
        if isinstance(v, dict):
            achado = _cavar(v, chaves, profundidade - 1, so_texto)
            if achado is not None:
                return achado
    return None


def _caiu_a_conexao(dados):
    """True quando o evento de conexao indica chip desconectado.

    Le os dois sinais: isconnected falso e modo 'qr'. Um sozinho da falso
    positivo -- 'qr' tambem aparece num pareamento que esta dando certo, e
    isconnected chega ausente em evento que nao e de conexao.
    """
    if not isinstance(dados, dict):
        return False
    status = dados.get("status")
    alvo = status if isinstance(status, dict) else dados

    conectado = _cavar(alvo, ("isconnected", "isConnected"))
    if conectado is False:
        return True
    modo = _cavar(alvo, ("mode",))
    expirado = _cavar(alvo, ("isqrcodeexpired", "isQrcodeExpired"))
    return str(modo).lower() == "qr" and conectado is not True and expirado is not None


def ler_evento(corpo):
    """Achata o payload do webhook no que interessa.

    O formato do DigiSac nao esta publicado em lugar que de para ler sem
    login, entao cada campo e procurado em mais de um lugar. Evento que nao
    encaixa devolve tipo vazio e e descartado pelo chamador -- melhor perder
    um status do que gravar lixo como se fosse entrega.
    """
    d = corpo or {}
    dados = d.get("data") or d.get("payload") or d

    tipo = (d.get("event") or d.get("type") or "").strip()

    msg_id = _cavar(dados, ("id", "messageId", "message_id")) or ""
    service_id = _cavar(dados, ("serviceId", "service_id")) or ""
    if isinstance(service_id, dict):
        service_id = service_id.get("id") or ""

    # "ack", "status" e "messageStatus" aparecem em integracoes diferentes do
    # mesmo produto; aceitar os tres evita depender de qual e o desta conta.
    estado = _cavar(dados, ("ack", "status", "messageStatus", "state")) or ""

    numero = _cavar(dados, ("number", "phone", "from", "to")) or ""

    return {
        "tipo": tipo,
        "msg_id": str(msg_id or ""),
        "service_id": str(service_id or ""),
        "estado": str(estado or "").lower(),
        "numero": "".join(c for c in str(numero) if c.isdigit()),
        "texto": str(_cavar(dados, ("text", "body", "message"),
                             so_texto=True) or ""),
        # isFromMe distingue o que NOS mandamos do que o lead respondeu. Sem
        # isso, a propria mensagem da campanha seria lida como resposta.
        "minha": bool(_cavar(dados, ("isFromMe", "fromMe")) or False),
        # service.updated com isconnected=false, ou pedindo QR, quer dizer que
        # o chip caiu do WhatsApp Web. Vem como evento proprio, antes de o
        # proximo envio falhar.
        "caiu": _caiu_a_conexao(dados),
    }


# Palavras que, vindas do lead, valem como pedido de parada. Conferidas
# contra a mensagem inteira e tambem como inicio dela: "PARE" e "pare de
# mandar" sao a mesma intencao.
PEDIDOS_DE_PARADA = (
    "pare", "parar", "para", "sair", "remover", "remova", "descadastrar",
    "descadastre", "cancelar", "nao quero", "não quero", "stop",
    "nao perturbe", "não perturbe", "me tira", "me tire",
)


def pede_parada(texto):
    """Resposta do lead pedindo para nao receber mais.

    Respeitar isto nao e so educacao: no WhatsApp, quem continua mandando
    depois de "pare" coleciona denuncia, e denuncia derruba o numero.
    """
    t = (texto or "").strip().lower()
    if not t or len(t) > 120:
        return False
    limpo = "".join(c if c.isalnum() or c.isspace() else " " for c in t)
    limpo = " ".join(limpo.split())
    return any(limpo == p or limpo.startswith(p + " ") for p in PEDIDOS_DE_PARADA)
