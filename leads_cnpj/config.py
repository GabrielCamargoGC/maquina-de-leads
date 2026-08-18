"""
Layout dos arquivos de Dados Abertos do CNPJ (Receita Federal).
Referencia: https://www.gov.br/receitafederal/dados/cnpj-metadados.pdf

Os arquivos sao CSV separados por ';', sem cabecalho, codificacao latin-1
(exceto onde detectado BOM utf-16/utf-8 -- ver buscar_leads.py).
"""

CACHE_DIR = "cache"

NEXTCLOUD_HOST = "https://arquivos.receitafederal.gov.br"
NEXTCLOUD_TOKEN = "YggdBLfdninEJX9"
NEXTCLOUD_WEBDAV = f"{NEXTCLOUD_HOST}/public.php/webdav"

CANDIDATE_BASES = [
    "https://dadosabertos.rfb.gov.br/CNPJ/",
    "https://dados-abertos-rf-cnpj.casadosdados.com.br/",
]

# Prefixos de arquivo que baixamos. Estabelecimentos e Empresas vem em ate
# 10 partes numeradas (Estabelecimentos0.zip ... Estabelecimentos9.zip).
# Municipios, Cnaes e Simples sao arquivo unico (sem numero).
PREFIXOS_MULTIPARTE = ["Estabelecimentos", "Empresas"]
ARQUIVOS_UNICOS = ["Municipios", "Cnaes", "Simples"]

# --- Layout: Estabelecimentos*.zip (30 colunas, indice 0-based) ---
EST_CNPJ_BASICO = 0
EST_CNPJ_ORDEM = 1
EST_CNPJ_DV = 2
EST_IDENT_MATRIZ_FILIAL = 3
EST_NOME_FANTASIA = 4
EST_SITUACAO_CADASTRAL = 5
EST_DATA_SITUACAO_CADASTRAL = 6
EST_MOTIVO_SITUACAO_CADASTRAL = 7
EST_NOME_CIDADE_EXTERIOR = 8
EST_PAIS = 9
EST_DATA_INICIO_ATIVIDADE = 10
EST_CNAE_PRINCIPAL = 11
EST_CNAE_SECUNDARIA = 12
EST_TIPO_LOGRADOURO = 13
EST_LOGRADOURO = 14
EST_NUMERO = 15
EST_COMPLEMENTO = 16
EST_BAIRRO = 17
EST_CEP = 18
EST_UF = 19
EST_MUNICIPIO = 20
EST_DDD1 = 21
EST_TELEFONE1 = 22
EST_DDD2 = 23
EST_TELEFONE2 = 24
EST_DDD_FAX = 25
EST_FAX = 26
EST_EMAIL = 27
EST_SITUACAO_ESPECIAL = 28
EST_DATA_SITUACAO_ESPECIAL = 29

SITUACAO_CADASTRAL_ATIVA = "02"
SITUACAO_CADASTRAL_DESCRICAO = {
    "01": "Nula",
    "02": "Ativa",
    "03": "Suspensa",
    "04": "Inapta",
    "08": "Baixada",
}

# --- Layout: Empresas*.zip (7 colunas) ---
EMP_CNPJ_BASICO = 0
EMP_RAZAO_SOCIAL = 1
EMP_NATUREZA_JURIDICA = 2
EMP_QUALIFICACAO_RESPONSAVEL = 3
EMP_CAPITAL_SOCIAL = 4
EMP_PORTE_EMPRESA = 5
EMP_ENTE_FEDERATIVO_RESPONSAVEL = 6

# --- Layout: Simples.zip (7 colunas, arquivo unico) ---
SIMPLES_CNPJ_BASICO = 0
SIMPLES_OPCAO_SIMPLES = 1
SIMPLES_DATA_OPCAO_SIMPLES = 2
SIMPLES_DATA_EXCLUSAO_SIMPLES = 3
SIMPLES_OPCAO_MEI = 4
SIMPLES_DATA_OPCAO_MEI = 5
SIMPLES_DATA_EXCLUSAO_MEI = 6

# --- Layout: Municipios.zip / Cnaes.zip (2 colunas: codigo;descricao) ---
LOOKUP_CODIGO = 0
LOOKUP_DESCRICAO = 1
