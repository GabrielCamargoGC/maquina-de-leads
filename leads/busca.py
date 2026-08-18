#!/usr/bin/env python3
r"""
Busca sobre o Parquet consolidado, via DuckDB.

Substitui a varredura de zip do buscar_leads.py antigo. A diferenca de fundo
nao e o SQL -- e o formato: como o Parquet e particionado por balde e UF, uma
busca em Assis-SP abre 10 arquivos pequenos e le so as colunas filtradas, em
vez de descomprimir 7,3 GB de zip para achar as mesmas linhas.

Uma conexao DuckDB e compartilhada pelo processo; cada thread do site pede
um cursor proprio (con.cursor()), que e a forma suportada de concorrencia.
"""
import threading
import unicodedata
from pathlib import Path

import duckdb

from . import config, consolidar

_lock = threading.Lock()
_con = None
_dir_carregado = None


def normalizar(texto):
    """Mesma normalizacao que o strip_accents/upper do DuckDB faz na
    gravacao -- os dois lados precisam bater para a comparacao funcionar."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto).strip().upper())
    return "".join(c for c in t if not unicodedata.combining(c))


def conexao(dir_dados=None):
    """Conexao unica, aberta sob demanda. Trocar de pasta (novo -> atual
    depois da troca mensal) reabre."""
    global _con, _dir_carregado
    dir_dados = Path(dir_dados or config.DIR_ATUAL)
    with _lock:
        if _con is not None and _dir_carregado == dir_dados:
            return _con
        if _con is not None:
            _con.close()
        con = duckdb.connect()
        con.execute(f"SET memory_limit='{config.DUCKDB_MEMORIA}'")
        con.execute(f"SET threads={config.DUCKDB_THREADS}")
        _con, _dir_carregado = con, dir_dados
        return _con


# balde e uf sao pastas, nao colunas dentro do arquivo. Sem fixar o tipo, o
# autodetect do DuckDB leria o balde "0" como numero e a comparacao com
# texto falharia. Os tipos tem que ser os mesmos da gravacao.
LEITURA = ("read_parquet('{}', hive_partitioning=1, hive_types="
           + consolidar.hive_tipos_sql() + ")")


def _caminhos(dir_dados=None):
    d = Path(dir_dados or config.DIR_ATUAL)
    return {
        "empresas": (d / "empresas_final" / "**" / "*.parquet").as_posix(),
        "cidades": (d / "cidades.parquet").as_posix(),
        "cnaes": (d / "cnaes.parquet").as_posix(),
    }


def base_pronta(dir_dados=None):
    d = Path(dir_dados or config.DIR_ATUAL)
    return (d / "empresas_final").exists() and (d / "cidades.parquet").exists()


# ------------------------------------------------------------ apoio


def resolver_cidades(nomes, uf=None, dir_dados=None):
    """Nome de cidade -> lista de (uf, codigo, nome).

    Serve para dois fins: validar o que o usuario digitou (com sugestao se
    errar) e descobrir a UF, que e o que permite ao DuckDB abrir so a
    particao certa em vez de varrer o Brasil.
    """
    if not nomes:
        return [], []
    cur = conexao(dir_dados).cursor()
    cam = _caminhos(dir_dados)
    alvos = [normalizar(n) for n in nomes if n and n.strip()]
    if not alvos:
        return [], []

    sql = f"SELECT uf, municipio_codigo, municipio FROM read_parquet('{cam['cidades']}') WHERE municipio_norm IN ("
    sql += ",".join("?" for _ in alvos) + ")"
    params = list(alvos)
    if uf:
        sql += " AND uf = ?"
        params.append(uf.strip().upper())
    achados = cur.execute(sql, params).fetchall()

    encontrados_norm = {normalizar(a[2]) for a in achados}
    faltando = [n for n, a in zip(nomes, alvos) if a not in encontrados_norm]

    sugestoes = []
    if faltando:
        for nome in faltando:
            alvo = normalizar(nome)
            s = cur.execute(
                f"SELECT DISTINCT municipio, uf FROM read_parquet('{cam['cidades']}') "
                f"WHERE municipio_norm LIKE ? ORDER BY municipio LIMIT 8",
                [f"%{alvo}%"],
            ).fetchall()
            sugestoes.extend(f"{m} ({u})" for m, u in s)
    return achados, sugestoes


def resolver_cnaes(termo, dir_dados=None):
    """Codigo, prefixo de codigo ou palavra-chave -> lista de codigos.

    Retorna (codigos, sugestoes). Se o termo nao bate em nada, sugere sem
    aplicar -- buscar o ramo errado calado e pior que nao achar nada.
    """
    if not termo or not termo.strip():
        return None, []
    cur = conexao(dir_dados).cursor()
    cam = _caminhos(dir_dados)
    alvo = termo.strip()

    if alvo.replace("-", "").replace("/", "").isdigit():
        prefixo = alvo.replace("-", "").replace("/", "")
        r = cur.execute(
            f"SELECT codigo FROM read_parquet('{cam['cnaes']}') WHERE codigo LIKE ?",
            [f"{prefixo}%"],
        ).fetchall()
        return [c[0] for c in r], []

    alvo_norm = normalizar(alvo)
    r = cur.execute(
        f"SELECT codigo FROM read_parquet('{cam['cnaes']}') "
        f"WHERE upper(strip_accents(descricao)) LIKE ?",
        [f"%{alvo_norm}%"],
    ).fetchall()
    if r:
        return [c[0] for c in r], []

    # Nada bateu. Sugere comparando PALAVRA a palavra, nao a descricao
    # inteira: quem digita "contabilidadi" quer bater em "contabilidade"
    # dentro de "Atividades de contabilidade", e a similaridade da frase
    # completa dilui isso a ponto de sugerir "Cultivo de cha-da-india".
    s = cur.execute(
        f"""SELECT descricao, max(jaro_winkler_similarity(palavra, ?)) AS sim
            FROM (
                SELECT descricao,
                       unnest(string_split(upper(strip_accents(descricao)), ' ')) AS palavra
                FROM read_parquet('{cam['cnaes']}')
            )
            WHERE length(palavra) >= 4
            GROUP BY descricao
            HAVING sim >= 0.75
            ORDER BY sim DESC LIMIT 8""",
        [alvo_norm],
    ).fetchall()
    return [], [x[0] for x in s]


# ------------------------------------------------------------ busca


COLUNAS_TELA = [
    "cnpj", "razao_social", "nome_fantasia", "situacao_desc",
    "cnae_principal", "cnae_descricao", "porte_desc",
    "optante_simples", "optante_mei",
    "logradouro", "numero", "complemento", "bairro", "cep",
    "municipio", "uf", "telefone_fmt", "ddd1", "telefone1",
    "ddd2", "telefone2", "email", "capital_social",
    "data_abertura", "matriz_filial_desc",
]


class Filtros:
    """Tudo opcional menos cidades ou uf -- sem um dos dois a busca varreria
    o Brasil inteiro, que nao e o que ninguem quer sem pedir."""

    def __init__(self, cidades=None, uf=None, bairro=None, cnae=None,
                 apenas_ativas=True, apenas_simples=False, apenas_mei=False,
                 com_telefone=False, com_email=False, portes=None,
                 capital_min=None, aberta_de=None, aberta_ate=None,
                 apenas_matriz=False):
        self.cidades = [c.strip() for c in (cidades or []) if c and c.strip()]
        self.uf = (uf or "").strip().upper() or None
        self.bairro = (bairro or "").strip() or None
        self.cnae = (cnae or "").strip() or None
        self.apenas_ativas = apenas_ativas
        self.apenas_simples = apenas_simples
        self.apenas_mei = apenas_mei
        self.com_telefone = com_telefone
        self.com_email = com_email
        self.portes = portes or []
        self.capital_min = capital_min
        self.aberta_de = aberta_de
        self.aberta_ate = aberta_ate
        self.apenas_matriz = apenas_matriz


class ErroBusca(Exception):
    """Erro que o usuario consegue corrigir (cidade errada, CNAE inexistente).
    Diferente de falha tecnica -- o site mostra a mensagem como aviso."""


def _montar_where(f, dir_dados, alias=None):
    """Devolve (clausula_where, params).

    alias prefixa as colunas ("a" -> "a.uf"), porque a tela de novidades
    compara duas bases na mesma consulta e ai toda coluna precisa dizer de
    qual das duas ela e.

    A condicao de UF vem primeiro de proposito: e ela que faz o DuckDB abrir
    so as particoes certas em vez de varrer o Brasil.
    """
    p = f"{alias}." if alias else ""
    cond, params = [], []

    cidades, sugestoes = resolver_cidades(f.cidades, f.uf, dir_dados) if f.cidades else ([], [])
    if f.cidades and not cidades:
        msg = f"Nenhum municipio encontrado para: {', '.join(f.cidades)}."
        if sugestoes:
            msg += " Voce quis dizer: " + ", ".join(sugestoes[:8]) + "?"
        raise ErroBusca(msg)

    if cidades:
        ufs = sorted({c[0] for c in cidades})
        cond.append(f"{p}uf IN (" + ",".join("?" for _ in ufs) + ")")
        params.extend(ufs)
        codigos = sorted({c[1] for c in cidades})
        cond.append(f"{p}municipio_codigo IN (" + ",".join("?" for _ in codigos) + ")")
        params.extend(codigos)
    elif f.uf:
        cond.append(f"{p}uf = ?")
        params.append(f.uf)
    else:
        raise ErroBusca("Informe pelo menos uma cidade ou uma UF.")

    if f.bairro:
        cond.append(f"{p}bairro_norm LIKE ?")
        params.append(f"%{normalizar(f.bairro)}%")

    if f.cnae:
        codigos, sugestoes = resolver_cnaes(f.cnae, dir_dados)
        if not codigos:
            msg = f"Nenhum CNAE encontrado para '{f.cnae}'."
            if sugestoes:
                msg += " Voce quis dizer: " + "; ".join(sugestoes[:5]) + "?"
            raise ErroBusca(msg)
        cond.append(f"{p}cnae_principal IN (" + ",".join("?" for _ in codigos) + ")")
        params.extend(codigos)

    if f.apenas_ativas:
        cond.append(f"{p}situacao_cadastral = '02'")
    if f.apenas_simples:
        cond.append(f"{p}optante_simples")
    if f.apenas_mei:
        cond.append(f"{p}optante_mei")
    if f.com_telefone:
        cond.append(f"{p}tem_telefone")
    if f.com_email:
        cond.append(f"{p}tem_email")
    if f.apenas_matriz:
        cond.append(f"{p}matriz_filial = '1'")
    if f.portes:
        cond.append(f"{p}porte_empresa IN (" + ",".join("?" for _ in f.portes) + ")")
        params.extend(f.portes)
    if f.capital_min is not None:
        cond.append(f"{p}capital_social >= ?")
        params.append(float(f.capital_min))
    if f.aberta_de:
        cond.append(f"{p}data_abertura >= ?")
        params.append(f.aberta_de)
    if f.aberta_ate:
        cond.append(f"{p}data_abertura <= ?")
        params.append(f.aberta_ate)

    return " AND ".join(cond), params


def contar(f, dir_dados=None):
    onde, params = _montar_where(f, dir_dados)
    cam = _caminhos(dir_dados)
    cur = conexao(dir_dados).cursor()
    sql = f"SELECT count(*) FROM {LEITURA.format(cam['empresas'])} WHERE {onde}"
    return cur.execute(sql, params).fetchone()[0]


def buscar(f, limite=None, offset=0, colunas=None, dir_dados=None):
    """Devolve lista de dicts. limite=None traz tudo (usado no export)."""
    onde, params = _montar_where(f, dir_dados)
    cam = _caminhos(dir_dados)
    cur = conexao(dir_dados).cursor()
    cols = ", ".join(colunas or COLUNAS_TELA)

    sql = (f"SELECT {cols} FROM {LEITURA.format(cam['empresas'])} "
           f"WHERE {onde} ORDER BY razao_social")
    if limite is not None:
        sql += f" LIMIT {int(limite)} OFFSET {int(offset)}"

    rel = cur.execute(sql, params)
    nomes = [d[0] for d in rel.description]
    return [dict(zip(nomes, linha)) for linha in rel.fetchall()]


def buscar_arrow(f, colunas=None, dir_dados=None):
    """Mesma busca, devolvendo Arrow em vez de dicts -- e o que o export usa
    para escrever CSV/Excel de 200 mil linhas sem montar tudo em memoria
    Python."""
    onde, params = _montar_where(f, dir_dados)
    cam = _caminhos(dir_dados)
    cur = conexao(dir_dados).cursor()
    cols = ", ".join(colunas or COLUNAS_TELA)
    sql = (f"SELECT {cols} FROM {LEITURA.format(cam['empresas'])} "
           f"WHERE {onde} ORDER BY razao_social")
    return cur.execute(sql, params).fetch_arrow_reader(batch_size=50_000)
