#!/usr/bin/env python3
r"""
Empresas que apareceram desde a base anterior.

O programa antigo resolvia isso guardando um JSON de CNPJs por busca, e so
sabia responder para combinacoes que alguem ja tinha consultado antes -- a
primeira consulta de um municipio nunca mostrava novidade, so criava o marco
inicial. Aqui a comparacao e entre as duas bases guardadas em disco
(dados/atual e dados/anterior), entao qualquer cidade responde na primeira
vez, para qualquer filtro, sem historico por usuario.

Enquanto dados/anterior nao existir (primeira carga da maquina), cai para um
criterio aproximado -- abertas nos ultimos 6 meses -- e sinaliza que e
aproximado, em vez de devolver lista vazia sem explicar.
"""
from datetime import date, timedelta
from pathlib import Path

from . import busca, config

DIAS_APROXIMADO = 180


def tem_base_anterior(dir_anterior=None):
    d = Path(dir_anterior or config.DIR_ANTERIOR)
    return (d / "empresas_final").exists()


def _leitura(dir_dados):
    caminho = (Path(dir_dados) / "empresas_final" / "**" / "*.parquet").as_posix()
    return busca.leitura(caminho)


def _sql(f, dir_atual, dir_anterior, colunas, ordem, limite):
    """Monta a consulta e diz se o resultado e exato ou aproximado."""
    if not tem_base_anterior(dir_anterior):
        onde, params = busca._montar_where(f, dir_atual)
        cols = ", ".join(colunas)
        sql = (f"SELECT {cols} FROM {_leitura(dir_atual)} "
               f"WHERE {onde} AND data_abertura >= ?")
        params = params + [date.today() - timedelta(days=DIAS_APROXIMADO)]
        aproximado = True
    else:
        onde, params = busca._montar_where(f, dir_atual, alias="a")
        # So prefixa nome de coluna. Uma expressao como "count(*)" viraria
        # "a.count(*)", que nao e SQL valido -- foi assim que a contagem de
        # novidades quebrou.
        cols = ", ".join(f"a.{c}" if c.isidentifier() else c for c in colunas)
        sql = (f"SELECT {cols} FROM {_leitura(dir_atual)} a "
               f"WHERE {onde} "
               f"  AND NOT EXISTS (SELECT 1 FROM {_leitura(dir_anterior)} b "
               f"                  WHERE b.cnpj_numerico = a.cnpj_numerico)")
        aproximado = False

    if ordem:
        sql += f" ORDER BY {ordem}"
    if limite:
        sql += f" LIMIT {int(limite)}"
    return sql, params, aproximado


def buscar_novas(f, limite=None, dir_atual=None, dir_anterior=None):
    """Devolve (linhas, aproximado).

    aproximado=True significa que nao havia base anterior para comparar --
    quem chama precisa avisar o usuario, porque a lista nao e a mesma coisa.
    """
    dir_atual = Path(dir_atual or config.DIR_ATUAL)
    dir_anterior = Path(dir_anterior or config.DIR_ANTERIOR)
    prefixo = "" if not tem_base_anterior(dir_anterior) else "a."
    sql, params, aproximado = _sql(
        f, dir_atual, dir_anterior, busca.COLUNAS_TELA,
        f"{prefixo}data_abertura DESC NULLS LAST", limite,
    )
    rel = busca.conexao(dir_atual).cursor().execute(sql, params)
    nomes = [d[0] for d in rel.description]
    return [dict(zip(nomes, linha)) for linha in rel.fetchall()], aproximado


def contar_novas(f, dir_atual=None, dir_anterior=None):
    dir_atual = Path(dir_atual or config.DIR_ATUAL)
    dir_anterior = Path(dir_anterior or config.DIR_ANTERIOR)
    sql, params, aproximado = _sql(
        f, dir_atual, dir_anterior, ["count(*)"], None, None
    )
    n = busca.conexao(dir_atual).cursor().execute(sql, params).fetchone()[0]
    return n, aproximado


def arrow_novas(f, colunas, dir_atual=None, dir_anterior=None):
    """Versao para export: devolve lotes Arrow em vez de dicts."""
    dir_atual = Path(dir_atual or config.DIR_ATUAL)
    dir_anterior = Path(dir_anterior or config.DIR_ANTERIOR)
    prefixo = "" if not tem_base_anterior(dir_anterior) else "a."
    sql, params, _ = _sql(
        f, dir_atual, dir_anterior, colunas,
        f"{prefixo}data_abertura DESC NULLS LAST", None,
    )
    cur = busca.conexao(dir_atual).cursor()
    return busca.leitor_arrow(cur.execute(sql, params))
