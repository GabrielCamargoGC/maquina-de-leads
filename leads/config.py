r"""
Caminhos e ajustes. Tudo sobrescrevivel por variavel de ambiente, para o
desktop (C:\leads) e esta maquina de desenvolvimento usarem o mesmo codigo.
"""
import os
from pathlib import Path

RAIZ = Path(os.environ.get("LEADS_RAIZ", Path(__file__).resolve().parent.parent))


def carregar_env(caminho=None):
    """Le o .env para dentro do ambiente do processo.

    Ate aqui so o instalar.ps1 abria esse arquivo, e apenas para pegar o
    token do tunel. Para o Python ele era decorativo: quem punha uma
    credencial ali via os.environ.get devolver vazio e nao tinha como
    descobrir por que -- o arquivo estava certo, so que ninguem lia.

    Quem ja esta no ambiente de verdade GANHA do arquivo. E o que permite ao
    servico, ou a um teste, sobrescrever um valor sem editar o disco.

    Leitura tolerante de proposito: o arquivo e escrito a mao, direto no
    servidor, muitas vezes por acesso remoto. Linha torta e ignorada em vez
    de derrubar a importacao do modulo -- e a importacao deste modulo derruba
    o site inteiro.
    """
    arquivo = Path(caminho) if caminho else RAIZ / ".env"
    try:
        if not arquivo.is_file():
            return 0
        bruto = arquivo.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return 0

    postos = 0
    for linha in bruto.splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        if linha.lower().startswith("export "):
            linha = linha[7:].lstrip()
        chave, _, valor = linha.partition("=")     # partition: valor pode ter '='
        chave = chave.strip()
        if not chave:
            continue
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        if chave not in os.environ:
            os.environ[chave] = valor
            postos += 1
    return postos


# Antes de qualquer os.environ.get abaixo -- senao o arquivo e lido depois de
# as constantes ja terem pego o valor vazio.
#
# O resultado fica guardado para o painel do Master poder dizer se o arquivo
# foi achado e quantas variaveis vieram dele. Sem isso, "faltam credenciais"
# nao distingue "nao escrevi" de "escrevi no lugar errado" -- que foi
# exatamente onde esta configuracao se perdeu da primeira vez.
ARQUIVO_ENV = RAIZ / ".env"
ENV_CARREGADAS = carregar_env()

# Zips crus baixados da Receita. Por padrao aponta para o cache que ja existe.
DIR_DOWNLOADS = Path(os.environ.get("LEADS_DOWNLOADS", RAIZ / "leads_cnpj" / "cache"))

# Parquet. 'atual' e o que o site le; 'novo' e onde o job monta; 'anterior'
# fica guardado para calcular quem abriu desde o mes passado.
DIR_DADOS = Path(os.environ.get("LEADS_DADOS", RAIZ / "dados"))
DIR_ATUAL = DIR_DADOS / "atual"
DIR_NOVO = DIR_DADOS / "novo"
DIR_ANTERIOR = DIR_DADOS / "anterior"

DIR_LOGS = Path(os.environ.get("LEADS_LOGS", RAIZ / "logs"))
DIR_EXPORTS = Path(os.environ.get("LEADS_EXPORTS", RAIZ / "exports"))
BANCO_APP = Path(os.environ.get("LEADS_BANCO_APP", RAIZ / "app.db"))

# --- Fonte na Receita ---
NEXTCLOUD_HOST = "https://arquivos.receitafederal.gov.br"
NEXTCLOUD_TOKEN = os.environ.get("LEADS_RFB_TOKEN", "YggdBLfdninEJX9")
BASES_LEGADO = [
    "https://dadosabertos.rfb.gov.br/CNPJ/",
    "https://dados-abertos-rf-cnpj.casadosdados.com.br/",
]

# --- Limites (desktop 8 GB de RAM) ---
# DuckDB derrama para disco ao passar disso, em vez de estourar a maquina.
# O site e a importacao tem orcamentos diferentes de proposito: o site
# divide a maquina com 15 pessoas, a importacao roda as 03:00 sozinha.
DUCKDB_MEMORIA = os.environ.get("LEADS_DUCKDB_MEMORIA", "2GB")
DUCKDB_THREADS = int(os.environ.get("LEADS_DUCKDB_THREADS", "4"))
# 5 GB. Ja rodou com 3 e com 5 na maquina de 8 GB, medido no mesmo dado:
#
#     teto 5 GB -> balde em  63s
#     teto 3 GB -> balde em 282s   (4,5x mais lento)
#
# A diferenca e derramamento para disco: com menos memoria o DuckDB escreve
# muito mais arquivo temporario. Os 3 GB foram tentativa de resolver um
# travamento que na verdade era um lock orfao -- com 5 GB sobrava RAM (3,3 GB
# livres no pico). As 03:00 a conversao roda praticamente sozinha; o site
# ocioso ocupa ~30 MB, nao os 2 GB do teto dele.
DUCKDB_MEMORIA_IMPORT = os.environ.get("LEADS_DUCKDB_MEMORIA_IMPORT", "5GB")
DUCKDB_THREADS_IMPORT = int(os.environ.get("LEADS_DUCKDB_THREADS_IMPORT", "3"))
# Lote de leitura do CSV. 64 MB equilibra velocidade e RAM.
BLOCO_CSV = 64 << 20

# Cookie de sessao so viaja por HTTPS. Vale sempre em producao; local, sem
# certificado, precisa ser desligado -- senao o navegador descarta o cookie e
# o login nunca fecha.
#
# Configuracao explicita, e nao "not app.debug", porque a protecao e ligada
# na importacao do modulo, antes de o modo de depuracao existir: a deducao
# automatica dava sempre "producao" e quebrava o desenvolvimento.
COOKIE_SEGURO = os.environ.get("LEADS_COOKIE_SEGURO", "1") not in ("0", "nao", "false")

WEB_PORTA = int(os.environ.get("LEADS_PORTA", "8080"))
WEB_THREADS = int(os.environ.get("LEADS_WEB_THREADS", "8"))
MAX_LINHAS_TELA = 300
EXPORTS_SIMULTANEOS = int(os.environ.get("LEADS_EXPORTS_SIMULTANEOS", "2"))


# --- DigiSac (disparo de WhatsApp) ---
#
# Tudo por ambiente: token e credencial e nao entra em Git nem em codigo.
# Vazio desliga o disparo -- a tela avisa em vez de quebrar.
DIGISAC_SUBDOMINIO = os.environ.get("DIGISAC_SUBDOMINIO", "").strip()
DIGISAC_TOKEN = os.environ.get("DIGISAC_TOKEN", "").strip()
DIGISAC_SERVICE_ID = os.environ.get("DIGISAC_SERVICE_ID", "").strip()

# Endereco publico do site, para montar a URL do webhook na tela do Master.
SITE_URL = os.environ.get("LEADS_SITE_URL", "https://zebrahads.com.br").rstrip("/")

# Ritmo do disparo. Os numeros sao os que a operacao ja usa no CRM: uma
# mensagem a cada 3 a 6 segundos, com o intervalo sorteado dentro da faixa.
#
# O sorteio nao e enfeite: cadencia exata e assinatura de robo. E o teto de 6
# nao pode subir muito -- a 4,5s medios, 5 mil numeros ja levam mais de 6
# horas, e campanha que atravessa a madrugada chega em horario que irrita.
DISPARO_PAUSA_MIN = float(os.environ.get("LEADS_DISPARO_PAUSA_MIN", "3"))
DISPARO_PAUSA_MAX = float(os.environ.get("LEADS_DISPARO_PAUSA_MAX", "6"))

# Teto por campanha. Trava de seguranca contra o erro de mandar para a
# cidade inteira sem perceber.
DISPARO_MAX_DESTINOS = int(os.environ.get("LEADS_DISPARO_MAX", "5000"))


def garantir_pastas():
    for d in (DIR_DADOS, DIR_LOGS, DIR_EXPORTS, DIR_DOWNLOADS):
        d.mkdir(parents=True, exist_ok=True)
