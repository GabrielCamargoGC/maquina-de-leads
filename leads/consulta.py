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
from pathlib import Path

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


def _indice(tipo, dir_dados=None):
    """Caminho do indice daquele tipo. Um arquivo por tipo: a consulta abre
    so o que interessa, e cada um e montado e conferido separadamente."""
    d = config.DIR_ATUAL if dir_dados is None else dir_dados
    return (d / "indice" / f"tipo={tipo}" / "dados.parquet").as_posix()


def tem_indice(dir_dados=None):
    d = config.DIR_ATUAL if dir_dados is None else dir_dados
    return (d / "indice").is_dir()


COLUNAS = busca.COLUNAS_TELA + [
    "cnpj_basico", "cnpj_numerico", "cnae_secundaria", "natureza_juridica",
    "data_situacao", "situacao_cadastral", "matriz_filial", "municipio_codigo",
]


def _por_cnpjs(cnpjs, dir_dados=None, limite=LIMITE_PADRAO, ufs=None):
    """Traz as linhas completas a partir de uma lista de CNPJ.

    ufs importa muito. A base e particionada por (balde, uf): o balde sai do
    proprio CNPJ, mas sem a UF o DuckDB precisa abrir a pasta de TODOS os
    estados de cada balde. Com 26 resultados espalhados isso vira quase as
    280 particoes, e a consulta levava 21 segundos depois de o indice ja ter
    respondido em milissegundos. Por isso o indice guarda a UF junto.
    """
    if not cnpjs:
        return []
    cur = busca.conexao(dir_dados).cursor()
    marcas = ",".join("?" for _ in cnpjs)
    baldes = sorted({c[7] for c in cnpjs if len(c) >= 8})
    cond_balde = ""
    if baldes:
        cond_balde = " AND balde IN (" + ",".join("?" for _ in baldes) + ")"
    ufs = sorted({u for u in (ufs or []) if u})
    if ufs:
        cond_balde += " AND uf IN (" + ",".join("?" for _ in ufs) + ")"
    sql = (f"SELECT {', '.join(COLUNAS)} FROM {_leitura(dir_dados)} "
           f"WHERE cnpj_numerico IN ({marcas}){cond_balde} "
           f"ORDER BY matriz_filial, cnpj_numerico LIMIT {int(limite)}")
    rel = cur.execute(sql, list(cnpjs) + baldes + ufs)
    nomes = [d[0] for d in rel.description]
    return [dict(zip(nomes, l)) for l in rel.fetchall()]


def _chaves(valor, tipo_indice, prefixo=False, limite=LIMITE_PADRAO,
            dir_dados=None):
    """CNPJs cujo nome/telefone bate, lendo o indice ordenado.

    Devolve None -- e nao lista vazia -- quando o indice nao pode ser lido.
    A diferenca importa: vazio significa "procurei e nao achei", None
    significa "nao consegui procurar aqui", e so o segundo justifica cair
    para a varredura da base.

    Indice corrompido ou pela metade nao pode derrubar a tela. Ja aconteceu:
    um arquivo truncado sobrou de uma geracao interrompida, a checagem de
    existencia deu positivo, e toda consulta por nome e telefone passou a
    responder erro.
    """
    cur = busca.conexao(dir_dados).cursor()
    caminho = _indice(tipo_indice, dir_dados)
    if not Path(caminho).exists():
        return None
    if prefixo:
        # intervalo em vez de LIKE: com >= e < o Parquet compara com o
        # minimo e o maximo de cada bloco e pula o que nao pode conter a
        # chave. Um LIKE 'X%' obrigaria a ler tudo.
        fim = valor[:-1] + chr(ord(valor[-1]) + 1) if valor else valor
        sql = (f"SELECT DISTINCT cnpj_numerico, uf FROM read_parquet('{caminho}') "
               f"WHERE chave >= ? AND chave < ? LIMIT {int(limite)}")
        params = [valor, fim]
    else:
        sql = (f"SELECT DISTINCT cnpj_numerico, uf FROM read_parquet('{caminho}') "
               f"WHERE chave = ? LIMIT {int(limite)}")
        params = [valor]
    try:
        return [(r[0], r[1]) for r in cur.execute(sql, params).fetchall()]
    except Exception:
        return None


def procurar(termo, limite=LIMITE_PADRAO, dir_dados=None, amplo=False):
    """Devolve (linhas, tipo, aviso).

    amplo=True procura o termo em QUALQUER parte do nome, e nao so no
    comeco. E o modo lento: nao existe ordenacao que ajude a achar trecho no
    meio de uma palavra, entao a coluna de nome da base inteira e lida. Fica
    como opcao na tela em vez de padrao -- quem procura "izabela" quase
    sempre quer os nomes que comecam assim, e esses saem em milissegundos.
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
        achados = _chaves(valor, "T", limite=limite, dir_dados=dir_dados)
        if achados is not None:
            return (_por_cnpjs([c for c, _ in achados], dir_dados, limite,
                               ufs=[u for _, u in achados]), tipo, None)
        sql = (f"SELECT {', '.join(COLUNAS)} FROM {_leitura(dir_dados)} "
               f"WHERE (ddd1 || telefone1) = ? OR (ddd2 || telefone2) = ? "
               f"LIMIT {int(limite)}")
        rel = cur.execute(sql, [valor, valor])
        nomes = [d[0] for d in rel.description]
        return ([dict(zip(nomes, l)) for l in rel.fetchall()], tipo,
                "Sem indice nesta base: a consulta por telefone varreu tudo.")

    # --- nome ---
    # Sempre por PREFIXO, nunca so exato. A busca exata era redundante --
    # "IZABELA" ja esta dentro do intervalo "IZABELA*" -- e, por rodar antes e
    # curto-circuitar quando achava algo, escondia todo nome que apenas
    # COMECA com o termo: procurar "izabela" achava as 26 empresas cujo
    # fantasia era exatamente "IZABELA" e nenhuma "IZABELA MODAS LTDA".
    if amplo:
        aviso = None
    else:
        por_razao = _chaves(valor, "R", prefixo=True, limite=limite, dir_dados=dir_dados)
        por_fantasia = _chaves(valor, "F", prefixo=True, limite=limite,
                               dir_dados=dir_dados)
        return _nome_pelo_indice(por_razao, por_fantasia, valor, tipo,
                                 dir_dados, limite, cur)

    return _nome_varrendo(valor, tipo, dir_dados, limite, cur, None)


def _nome_pelo_indice(por_razao, por_fantasia, valor, tipo, dir_dados, limite, cur):
    aviso = None
    if por_razao is not None or por_fantasia is not None:
        vistos, pares = set(), []
        for c, u in list(por_razao or []) + list(por_fantasia or []):
            if c not in vistos:
                vistos.add(c)
                pares.append((c, u))
        if pares:
            return (_por_cnpjs([c for c, _ in pares], dir_dados, limite,
                               ufs=[u for _, u in pares]), tipo, None)
        aviso = ("Nenhum nome comeca assim. Procurei tambem no meio do nome, "
                 "o que demora mais.")

    return _nome_varrendo(valor, tipo, dir_dados, limite, cur, aviso)


def _nome_varrendo(valor, tipo, dir_dados, limite, cur, aviso):
    """Trecho em qualquer parte do nome. Le a coluna de nome da base toda --
    nao ha ordenacao que ajude a achar no meio de uma palavra."""
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
