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


def testar():
    """(ok, mensagem). Serve para a tela dizer se as credenciais prestam
    ANTES de comecar uma campanha de horas."""
    if not configurado():
        return False, "Faltam DIGISAC_SUBDOMINIO, DIGISAC_TOKEN ou DIGISAC_SERVICE_ID no .env"
    try:
        _chamar("/contacts?perPage=1", metodo="GET")
        return True, "Conectado"
    except ErroDigiSac as e:
        return False, str(e)


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
