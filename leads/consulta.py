#!/usr/bin/env python3
r"""
Consulta pontual: acha UMA empresa (ou poucas) a partir do que a pessoa
digitou, sem escolher filtro nenhum.

A aba de Busca serve para montar lista de prospeccao. Esta serve para o
oposto: ja se sabe quem se procura, falta achar. Um campo so, e o sistema
descobre o que foi digitado.

Por que cada caso custa diferente:

  CNPJ      instantaneo. A base e particionada por BALDE, que e o ultimo
            digito do cnpj_basico -- procurar um CNPJ abre 1 dos 10 baldes.
            Nao foi planejado para isso; deu certo por consequencia.
  TELEFONE  e NOME EXATO/COMECA COM: passam pelo indice ordenado, que
            permite ao Parquet pular quase todos os blocos.
  NOME QUE  nao tem como ordenar por "meio da palavra", entao varre a coluna
  CONTEM    de nome da base principal. E o unico caso lento, e a tela avisa.
"""
import re
import unicodedata

from . import busca, config

LIMITE_PADRAO = 60

# O que a deteccao concluiu, para a tela poder explicar.
CNPJ_COMPLETO = "cnpj"
CNPJ_RAIZ = "raiz"
TELEFONE = "telefone"
NOME = "nome"

DESCRICAO = {
    CNPJ_COMPLETO: "CNPJ",
    CNPJ_RAIZ: "raiz de CNPJ (matriz e filiais)",
    TELEFONE: "telefone",
    NOME: "nome",
}


def normalizar(texto):
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto).strip().upper())
    return "".join(c for c in t if not unicodedata.combining(c))


def so_digitos(texto):
    return re.sub(r"\D", "", texto or "")


def detectar(termo):
    """Devolve (tipo, valor_limpo).

    A regra e pelo formato, nao por menu: 14 digitos e CNPJ, 8 e raiz,
    10 ou 11 e telefone, qualquer coisa com letra e nome. Digitar
    "11.222.333/0001-81" ou "11222333000181" da no mesmo.
    """
    termo = (termo or "").strip()
    if not termo:
        return None, ""

    digitos = so_digitos(termo)
    tem_letra = any(c.isalpha() for c in termo)

    if not tem_letra and digitos:
        if len(digitos) == 14:
            return CNPJ_COMPLETO, digitos
        if len(digitos) == 8:
            return CNPJ_RAIZ, digitos
        if len(digitos) in (10, 11):
            return TELEFONE, digitos
        # 12 ou 13 digitos costuma ser CNPJ com digito faltando ou sobrando;
        # tratar como raiz pelos 8 primeiros acha a empresa do mesmo jeito
        if 9 <= len(digitos) <= 13:
            return CNPJ_RAIZ, digitos[:8]

    return NOME, normalizar(termo)


def _leitura(dir_dados=None):
    d = config.DIR_ATUAL if dir_dados is None else dir_dados
    return busca.leitura((d / "empresas_final" / "**" / "*.parquet").as_posix())


def _indice(dir_dados=None):
    d = config.DIR_ATUAL if dir_dados is None else dir_dados
    return (d / "indice.parquet").as_posix()


def tem_indice(dir_dados=None):
    d = config.DIR_ATUAL if dir_dados is None else dir_dados
    return (d / "indice.parquet").exists()


COLUNAS = busca.COLUNAS_TELA + [
    "cnpj_basico", "cnpj_numerico", "cnae_secundaria", "natureza_juridica",
    "data_situacao", "situacao_cadastral", "matriz_filial", "municipio_codigo",
]


def _por_cnpjs(cnpjs, dir_dados=None, limite=LIMITE_PADRAO):
    """Traz as linhas completas a partir de uma lista de CNPJ.

    Rapido de proposito: o balde da particao sai do proprio CNPJ, entao o
    DuckDB abre so as pastas que podem conter aqueles numeros.
    """
    if not cnpjs:
        return []
    cur = busca.conexao(dir_dados).cursor()
    marcas = ",".join("?" for _ in cnpjs)
    baldes = sorted({c[7] for c in cnpjs if len(c) >= 8})
    cond_balde = ""
    if baldes:
        cond_balde = " AND balde IN (" + ",".join("?" for _ in baldes) + ")"
    sql = (f"SELECT {', '.join(COLUNAS)} FROM {_leitura(dir_dados)} "
           f"WHERE cnpj_numerico IN ({marcas}){cond_balde} "
           f"ORDER BY matriz_filial, cnpj_numerico LIMIT {int(limite)}")
    rel = cur.execute(sql, list(cnpjs) + baldes)
    nomes = [d[0] for d in rel.description]
    return [dict(zip(nomes, l)) for l in rel.fetchall()]


def _chaves(valor, tipo_indice, prefixo=False, limite=LIMITE_PADRAO):
    """CNPJs cujo nome/telefone bate, lendo o indice ordenado."""
    cur = busca.conexao().cursor()
    caminho = _indice()
    if prefixo:
        # intervalo em vez de LIKE: com >= e < o Parquet compara com o
        # minimo e o maximo de cada bloco e pula o que nao pode conter a
        # chave. Um LIKE 'X%' obrigaria a ler tudo.
        fim = valor[:-1] + chr(ord(valor[-1]) + 1) if valor else valor
        sql = (f"SELECT DISTINCT cnpj_numerico FROM read_parquet('{caminho}') "
               f"WHERE chave >= ? AND chave < ? AND tipo = ? LIMIT {int(limite)}")
        params = [valor, fim, tipo_indice]
    else:
        sql = (f"SELECT DISTINCT cnpj_numerico FROM read_parquet('{caminho}') "
               f"WHERE chave = ? AND tipo = ? LIMIT {int(limite)}")
        params = [valor, tipo_indice]
    return [r[0] for r in cur.execute(sql, params).fetchall()]


def procurar(termo, limite=LIMITE_PADRAO, dir_dados=None):
    """Devolve (linhas, tipo, aviso).

    aviso e texto para a tela quando algo precisa ser explicado -- consulta
    lenta, resultado cortado, indice ausente.
    """
    tipo, valor = detectar(termo)
    if not tipo:
        return [], None, None

    cur = busca.conexao(dir_dados).cursor()
    aviso = None

    if tipo == CNPJ_COMPLETO:
        return _por_cnpjs([valor], dir_dados, limite), tipo, None

    if tipo == CNPJ_RAIZ:
        # matriz e filiais compartilham os 8 primeiros digitos; o balde sai
        # do 8o, entao isto continua abrindo um balde so
        sql = (f"SELECT {', '.join(COLUNAS)} FROM {_leitura(dir_dados)} "
               f"WHERE cnpj_basico = ? AND balde = ? "
               f"ORDER BY matriz_filial, cnpj_numerico LIMIT {int(limite)}")
        rel = cur.execute(sql, [valor, valor[7]])
        nomes = [d[0] for d in rel.description]
        return [dict(zip(nomes, l)) for l in rel.fetchall()], tipo, None

    if tipo == TELEFONE:
        if tem_indice(dir_dados):
            return _por_cnpjs(_chaves(valor, "T", limite=limite),
                              dir_dados, limite), tipo, None
        sql = (f"SELECT {', '.join(COLUNAS)} FROM {_leitura(dir_dados)} "
               f"WHERE (ddd1 || telefone1) = ? OR (ddd2 || telefone2) = ? "
               f"LIMIT {int(limite)}")
        rel = cur.execute(sql, [valor, valor])
        nomes = [d[0] for d in rel.description]
        return ([dict(zip(nomes, l)) for l in rel.fetchall()], tipo,
                "Sem indice nesta base: a consulta por telefone varreu tudo.")

    # --- nome ---
    if tem_indice(dir_dados):
        cnpjs = _chaves(valor, "R", limite=limite) or []
        cnpjs += [c for c in _chaves(valor, "F", limite=limite) if c not in cnpjs]
        if not cnpjs:
            cnpjs = _chaves(valor, "R", prefixo=True, limite=limite) or []
            cnpjs += [c for c in _chaves(valor, "F", prefixo=True, limite=limite)
                      if c not in cnpjs]
        if cnpjs:
            return _por_cnpjs(cnpjs, dir_dados, limite), tipo, None
        aviso = ("Nenhum nome comeca assim. Procurei o trecho no meio do nome "
                 "tambem, o que demora mais.")

    # trecho no meio: nao ha ordenacao que ajude, entao le a coluna de nome
    # da base principal -- que para este caso e menor que o proprio indice
    sql = (f"SELECT {', '.join(COLUNAS)} FROM {_leitura(dir_dados)} "
           f"WHERE upper(strip_accents(razao_social)) LIKE ? "
           f"   OR upper(strip_accents(nome_fantasia)) LIKE ? "
           f"LIMIT {int(limite)}")
    alvo = f"%{valor}%"
    rel = cur.execute(sql, [alvo, alvo])
    nomes = [d[0] for d in rel.description]
    return [dict(zip(nomes, l)) for l in rel.fetchall()], tipo, aviso


def irmas(cnpj_basico, cnpj_atual, dir_dados=None, limite=200):
    """Outros estabelecimentos da mesma empresa.

    E o que a tela de filtro nao mostra hoje: que aquele endereco e a filial
    3 de 12, e onde estao as outras.
    """
    if not cnpj_basico or len(cnpj_basico) < 8:
        return []
    cur = busca.conexao(dir_dados).cursor()
    sql = (f"SELECT cnpj, cnpj_numerico, matriz_filial_desc, municipio, uf, "
           f"       situacao_desc, bairro "
           f"FROM {_leitura(dir_dados)} "
           f"WHERE cnpj_basico = ? AND balde = ? AND cnpj_numerico <> ? "
           f"ORDER BY matriz_filial, cnpj_numerico LIMIT {int(limite)}")
    rel = cur.execute(sql, [cnpj_basico, cnpj_basico[7], cnpj_atual or ""])
    nomes = [d[0] for d in rel.description]
    return [dict(zip(nomes, l)) for l in rel.fetchall()]


def cnaes_secundarios(bruto, dir_dados=None, limite=40):
    """A Receita entrega os CNAEs secundarios num campo so, separados por
    virgula ("4712100,4721102,..."). Aqui viram lista com descricao.

    Codigo sem descricao ainda aparece: a tabela de CNAEs muda de tempos em
    tempos e sumir com a linha esconderia uma atividade que a empresa
    declarou de verdade.
    """
    if not bruto:
        return []
    codigos = [c.strip() for c in str(bruto).split(",") if c.strip()][:limite]
    if not codigos:
        return []

    d = config.DIR_ATUAL if dir_dados is None else dir_dados
    caminho = (d / "cnaes.parquet").as_posix()
    mapa = {}
    try:
        cur = busca.conexao(dir_dados).cursor()
        marcas = ",".join("?" for _ in codigos)
        linhas = cur.execute(
            f"SELECT codigo, descricao FROM read_parquet('{caminho}') "
            f"WHERE codigo IN ({marcas})", codigos
        ).fetchall()
        mapa = {c: d for c, d in linhas}
    except Exception:
        pass  # sem a tabela de CNAEs, mostra so os codigos

    return [{"codigo": c, "descricao": mapa.get(c, "")} for c in codigos]


def uma(cnpj_numerico, dir_dados=None):
    linhas = _por_cnpjs([so_digitos(cnpj_numerico)], dir_dados, limite=1)
    return linhas[0] if linhas else None
