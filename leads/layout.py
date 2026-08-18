"""
Layout dos arquivos de Dados Abertos do CNPJ (Receita Federal).
Referencia: https://www.gov.br/receitafederal/dados/cnpj-metadados.pdf

CSV sem cabecalho, separado por ';', aspas duplas, latin-1.
Aqui os campos viram NOMES (o pyarrow le por nome), em vez dos indices
numericos que o leads_cnpj/config.py usava.
"""

EST_COLUNAS = [
    "cnpj_basico", "cnpj_ordem", "cnpj_dv", "matriz_filial", "nome_fantasia",
    "situacao_cadastral", "data_situacao_cadastral", "motivo_situacao_cadastral",
    "nome_cidade_exterior", "pais", "data_inicio_atividade",
    "cnae_principal", "cnae_secundaria",
    "tipo_logradouro", "logradouro", "numero", "complemento", "bairro", "cep",
    "uf", "municipio",
    "ddd1", "telefone1", "ddd2", "telefone2", "ddd_fax", "fax", "email",
    "situacao_especial", "data_situacao_especial",
]

EMP_COLUNAS = [
    "cnpj_basico", "razao_social", "natureza_juridica",
    "qualificacao_responsavel", "capital_social", "porte_empresa",
    "ente_federativo_responsavel",
]

SIMPLES_COLUNAS = [
    "cnpj_basico", "opcao_simples", "data_opcao_simples", "data_exclusao_simples",
    "opcao_mei", "data_opcao_mei", "data_exclusao_mei",
]

LOOKUP_COLUNAS = ["codigo", "descricao"]

# Prefixos dos zips na origem
PREFIXOS_MULTIPARTE = ["Estabelecimentos", "Empresas"]
ARQUIVOS_UNICOS = ["Municipios", "Cnaes", "Simples"]

SITUACAO_ATIVA = "02"
SITUACAO_DESCRICAO = {
    "01": "Nula", "02": "Ativa", "03": "Suspensa",
    "04": "Inapta", "08": "Baixada",
}

PORTE_DESCRICAO = {
    "00": "Nao informado", "01": "Micro empresa",
    "03": "Empresa de pequeno porte", "05": "Demais",
}

MATRIZ_FILIAL_DESCRICAO = {"1": "Matriz", "2": "Filial"}

# Quantos baldes de cnpj_basico. Ver importador.coluna_balde para o porque.
BALDES = 10

# Colunas que sobrevivem no parquet final de estabelecimentos.
# nome_cidade_exterior, pais, fax e situacao_especial saem: nunca usados
# na busca e so ocupam espaco.
EST_COLUNAS_MANTIDAS = [
    "cnpj_basico", "cnpj_ordem", "cnpj_dv", "matriz_filial", "nome_fantasia",
    "situacao_cadastral", "data_situacao_cadastral", "data_inicio_atividade",
    "cnae_principal", "cnae_secundaria",
    "tipo_logradouro", "logradouro", "numero", "complemento", "bairro", "cep",
    "uf", "municipio",
    "ddd1", "telefone1", "ddd2", "telefone2", "email",
]
