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


def _chamar(caminho, corpo=None, metodo="POST"):
    dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(
        _base() + caminho, data=dados, method=metodo,
        headers={
            "Authorization": f"Bearer {config.DIGISAC_TOKEN}",
            "Content-Type": "application/json",
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
        # 4xx e erro nosso (numero, token, payload): repetir da o mesmo.
        # 5xx e do lado deles e costuma passar.
        raise ErroDigiSac(str(msg), codigo=e.code, definitivo=400 <= e.code < 500)
    except urllib.error.URLError as e:
        raise ErroDigiSac(f"Sem resposta do DigiSac: {e.reason}", definitivo=False)
    except json.JSONDecodeError:
        raise ErroDigiSac("Resposta do DigiSac nao era JSON", definitivo=False)


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

    msg_id = dados.get("id") or dados.get("messageId") or ""
    service_id = (dados.get("serviceId") or dados.get("service_id")
                  or (dados.get("service") or {}).get("id") or "")

    # "ack", "status" e "messageStatus" aparecem em integracoes diferentes do
    # mesmo produto; aceitar os tres evita depender de qual e o desta conta.
    estado = (dados.get("ack") or dados.get("status")
              or dados.get("messageStatus") or "")

    contato = dados.get("contact") or {}
    numero = (dados.get("number") or contato.get("number")
              or contato.get("phone") or "")

    return {
        "tipo": tipo,
        "msg_id": str(msg_id or ""),
        "service_id": str(service_id or ""),
        "estado": str(estado or "").lower(),
        "numero": "".join(c for c in str(numero) if c.isdigit()),
        "texto": str(dados.get("text") or ""),
        # isFromMe distingue o que NOS mandamos do que o lead respondeu. Sem
        # isso, a propria mensagem da campanha seria lida como resposta.
        "minha": bool(dados.get("isFromMe") or dados.get("fromMe")),
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
