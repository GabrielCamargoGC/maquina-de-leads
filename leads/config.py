r"""
Caminhos e ajustes. Tudo sobrescrevivel por variavel de ambiente, para o
desktop (C:\leads) e esta maquina de desenvolvimento usarem o mesmo codigo.
"""
import os
from pathlib import Path

RAIZ = Path(os.environ.get("LEADS_RAIZ", Path(__file__).resolve().parent.parent))

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
# 3 GB, nao 5: o site reserva ate 2 GB e o Windows fica com ~2 GB, entao 5
# so caberia se a conversao rodasse absolutamente sozinha. Na pratica ela
# roda com o site no ar, e passar do total fisico faz a maquina paginar em
# disco -- que parece travamento, nao lentidao. Menos memoria significa mais
# escrita temporaria em disco e alguns minutos a mais, o que as 03:00 nao
# faz diferenca nenhuma.
DUCKDB_MEMORIA_IMPORT = os.environ.get("LEADS_DUCKDB_MEMORIA_IMPORT", "3GB")
DUCKDB_THREADS_IMPORT = int(os.environ.get("LEADS_DUCKDB_THREADS_IMPORT", "3"))
# Lote de leitura do CSV. 64 MB equilibra velocidade e RAM.
BLOCO_CSV = 64 << 20

WEB_PORTA = int(os.environ.get("LEADS_PORTA", "8080"))
WEB_THREADS = int(os.environ.get("LEADS_WEB_THREADS", "8"))
MAX_LINHAS_TELA = 300
EXPORTS_SIMULTANEOS = int(os.environ.get("LEADS_EXPORTS_SIMULTANEOS", "2"))


def garantir_pastas():
    for d in (DIR_DADOS, DIR_LOGS, DIR_EXPORTS, DIR_DOWNLOADS):
        d.mkdir(parents=True, exist_ok=True)
