#!/usr/bin/env python3
r"""
Etapa 5: junta as quatro partes importadas num Parquet unico, ja com os
campos calculados que a busca usa.

HISTORIA DESTE ARQUIVO (duas versoes morreram estourando os 8 GB de RAM):

  v1  uma juncao por UF. Relia empresas.parquet (69 M linhas) 28 vezes e
      morreu no 8o estado.
  v2  passe unico com ORDER BY municipio. Ordenar 72 M de linhas com
      endereco e razao social exige materializar tudo. Morreu tambem.
  v3  (esta) juncao por BALDE de cnpj_basico. Os tres arquivos grandes ja
      vem divididos em 10 baldes pelo importador, pelo mesmo criterio, entao
      cada juncao cruza ~7 M x ~7 M em vez de 72 M x 69 M. Dez juncoes
      pequenas, cada arquivo lido uma unica vez, nenhuma ordenacao global.

A saida sai particionada por (balde, uf): 10 x 27 = ~280 arquivos. Filtrar
por UF abre 10 arquivos; filtrar por cidade dentro deles e resolvido pela
leitura colunar do Parquet, que le a coluna do municipio antes de tocar no
resto da linha.

Uso:
    python -m leads.consolidar
    python -m leads.consolidar --ufs SP,PR
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

import duckdb

from . import config, layout

COMPRESSAO = "zstd"

# Colunas que viram caminho de pasta em vez de dado dentro do arquivo.
# Precisam ser lidas de volta como texto: se o DuckDB adivinhar, "0" vira
# numero e a comparacao com o texto que vem do indice de cidades falha.
HIVE_TIPOS = {"balde": "VARCHAR", "uf": "VARCHAR"}

SQL_SELECT = """
SELECT
    e.cnpj_basico,
    e.cnpj_ordem,
    e.cnpj_dv,
    e.cnpj_basico || e.cnpj_ordem || e.cnpj_dv                      AS cnpj_numerico,
    substr(e.cnpj_basico,1,2) || '.' || substr(e.cnpj_basico,3,3) || '.' ||
      substr(e.cnpj_basico,6,3) || '/' || e.cnpj_ordem || '-' || e.cnpj_dv
                                                                    AS cnpj,
    em.razao_social,
    e.nome_fantasia,
    e.matriz_filial,
    CASE e.matriz_filial WHEN '1' THEN 'Matriz' WHEN '2' THEN 'Filial' ELSE '' END
                                                                    AS matriz_filial_desc,

    e.situacao_cadastral,
    CASE e.situacao_cadastral
        WHEN '01' THEN 'Nula'    WHEN '02' THEN 'Ativa'  WHEN '03' THEN 'Suspensa'
        WHEN '04' THEN 'Inapta'  WHEN '08' THEN 'Baixada'
        ELSE e.situacao_cadastral END                                AS situacao_desc,

    e.cnae_principal,
    cn.descricao                                                     AS cnae_descricao,
    e.cnae_secundaria,

    em.porte_empresa,
    CASE em.porte_empresa
        WHEN '00' THEN 'Nao informado' WHEN '01' THEN 'Micro empresa'
        WHEN '03' THEN 'Pequeno porte' WHEN '05' THEN 'Demais'
        ELSE '' END                                                  AS porte_desc,
    em.natureza_juridica,
    TRY_CAST(replace(em.capital_social, ',', '.') AS DOUBLE)         AS capital_social,

    -- coalesce e obrigatorio: quem nao aparece na tabela do Simples sai do
    -- LEFT JOIN como NULL, e NULL nao e "nao optante" -- viraria celula
    -- vazia na planilha e sumiria de qualquer filtro que negue a condicao.
    coalesce(s.opcao_simples = 'S', false)                           AS optante_simples,
    coalesce(s.opcao_mei = 'S', false)                               AS optante_mei,

    trim(coalesce(e.tipo_logradouro,'') || ' ' || coalesce(e.logradouro,'')) AS logradouro,
    e.numero,
    e.complemento,
    e.bairro,
    upper(strip_accents(coalesce(e.bairro,'')))                      AS bairro_norm,
    e.cep,
    e.municipio                                                      AS municipio_codigo,
    mu.descricao                                                     AS municipio,
    upper(strip_accents(coalesce(mu.descricao,'')))                  AS municipio_norm,

    e.ddd1, e.telefone1, e.ddd2, e.telefone2,
    CASE WHEN e.telefone1 <> '' THEN '(' || e.ddd1 || ') ' || e.telefone1 ELSE '' END
                                                                     AS telefone_fmt,
    e.email,
    (e.telefone1 <> '' OR e.telefone2 <> '')                         AS tem_telefone,
    (e.email <> '')                                                  AS tem_email,

    try_strptime(e.data_inicio_atividade, '%Y%m%d')::DATE            AS data_abertura,
    try_strptime(e.data_situacao_cadastral, '%Y%m%d')::DATE          AS data_situacao,

    -- por ultimo: viram nome de pasta, nao coluna dentro do arquivo
    '{balde}'                                                        AS balde,
    e.uf                                                             AS uf

FROM read_parquet($est) e
LEFT JOIN read_parquet($empresas)   em ON em.cnpj_basico = e.cnpj_basico
LEFT JOIN read_parquet($simples)    s  ON s.cnpj_basico  = e.cnpj_basico
LEFT JOIN read_parquet($municipios) mu ON mu.codigo      = e.municipio
LEFT JOIN read_parquet($cnaes)      cn ON cn.codigo      = e.cnae_principal
"""


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _fmt(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def _tamanho(p):
    p = Path(p)
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def hive_tipos_sql():
    return "{" + ", ".join(f"'{k}': '{v}'" for k, v in HIVE_TIPOS.items()) + "}"


def conectar(temp_dir=None, memoria=None, threads=None):
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memoria or config.DUCKDB_MEMORIA_IMPORT}'")
    con.execute(f"SET threads={threads or config.DUCKDB_THREADS_IMPORT}")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET enable_progress_bar=false")
    if temp_dir:
        temp = Path(temp_dir).resolve()
        temp.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory='{temp.as_posix()}'")
    return con


def _marca(destino, balde):
    """Arquivo que diz 'este balde terminou inteiro'.

    A pasta do balde existir nao basta: se o processo morre no meio da
    gravacao, ela existe com dado pela metade. A marca so e escrita depois
    que o COPY retorna, entao ela e a unica prova de que aquele balde esta
    completo.
    """
    return destino / f".balde-{balde}.ok"


def consolidar(dir_entrada, ufs=None, retomar=False):
    entrada = Path(dir_entrada).resolve()

    for nome in ("estabelecimentos", "empresas", "simples"):
        if not (entrada / nome).exists():
            sys.exit(f"[erro] falta {entrada / nome}. Rode o importador primeiro.")
    for nome in ("municipios.parquet", "cnaes.parquet"):
        if not (entrada / nome).exists():
            sys.exit(f"[erro] falta {entrada / nome}. Rode a etapa 1 do importador.")

    destino = entrada / "empresas_final"
    if retomar and destino.exists():
        prontos = sorted(p.name[7:-3] for p in destino.glob(".balde-*.ok"))
        _log(f"--retomar: {len(prontos)} balde(s) ja completos "
             f"({', '.join(prontos) if prontos else 'nenhum'})")
    else:
        shutil.rmtree(destino, ignore_errors=True)
    destino.mkdir(parents=True, exist_ok=True)

    filtro_uf = ""
    if ufs:
        lista = ", ".join(f"'{u.strip().upper()}'" for u in ufs)
        filtro_uf = f" WHERE e.uf IN ({lista})"

    con = conectar(temp_dir=entrada / "_tmp")
    t0 = time.time()
    _log(f"Consolidando em {layout.BALDES} baldes (juncao pequena por vez)")

    baldes_feitos = 0
    for b in range(layout.BALDES):
        balde = str(b)
        pasta = entrada / "estabelecimentos" / f"balde={balde}"
        if not pasta.exists():
            continue  # teste com poucas UFs pode nao ter todos os baldes

        if retomar and _marca(destino, balde).exists():
            _log(f"  balde {balde}: ja estava completo, pulando")
            baldes_feitos += 1
            continue

        # Balde incompleto de uma execucao anterior: a pasta pode ter dado
        # pela metade. Apaga antes de refazer, senao sobra linha duplicada.
        for uf_dir in destino.glob(f"balde={balde}/*"):
            shutil.rmtree(uf_dir, ignore_errors=True)

        t_b = time.time()
        con.execute(
            f"COPY ({SQL_SELECT.format(balde=balde)}{filtro_uf}) TO '{destino.as_posix()}' "
            f"(FORMAT PARQUET, COMPRESSION {COMPRESSAO}, "
            f" PARTITION_BY (balde, uf), OVERWRITE_OR_IGNORE)",
            {
                "est": str(pasta / "*.parquet"),
                "empresas": str(entrada / "empresas" / f"balde={balde}" / "*.parquet"),
                "simples": str(entrada / "simples" / f"balde={balde}" / "*.parquet"),
                "municipios": str(entrada / "municipios.parquet"),
                "cnaes": str(entrada / "cnaes.parquet"),
            },
        )
        _marca(destino, balde).write_text("ok", encoding="utf-8")
        baldes_feitos += 1
        _log(f"  balde {balde}: {time.time()-t_b:.0f}s "
             f"(acumulado {_fmt(_tamanho(destino))})")

    if not baldes_feitos:
        sys.exit("[erro] nenhum balde encontrado em estabelecimentos/.")

    total = gerar_indice_cidades(con, entrada, destino)
    gerar_indice_consulta(con, entrada, destino)
    validar(con, entrada, destino)

    con.close()
    shutil.rmtree(entrada / "_tmp", ignore_errors=True)
    _log(f"Consolidado: {total:,} linhas em {time.time()-t0:.0f}s "
         f"-> {destino} ({_fmt(_tamanho(destino))})")
    return total


def conectar_manutencao(entrada):
    """Conexao para tarefa que roda com o SITE NO AR.

    Teto baixo de proposito. A conexao de importacao pede 5 GB, o que faz
    sentido as 03:00, quando ela e a unica coisa acontecendo. Rodada durante
    o expediente, esses 5 GB somados aos 2 GB do site e aos ~2 GB do Windows
    passam dos 8 GB da maquina: ela comeca a paginar em disco, o site para de
    responder e o Cloudflare corta a conexao com 524.

    Aconteceu de verdade ao gerar o indice durante o dia. Mais lento e
    aceitavel; derrubar o site para quem esta trabalhando nao e.
    """
    return conectar(temp_dir=entrada / "_tmp", memoria="2GB", threads=2)


def gerar_indice_consulta(con, entrada, destino, progresso=None):
    """indice.parquet: chave -> CNPJ, para a tela de Consulta.

    Uma linha por razao social, nome fantasia e telefone. A chave vai
    normalizada (maiuscula, sem acento; telefone so com digitos) e o arquivo
    sai ORDENADO por ela -- e a ordenacao que faz o truque: o Parquet guarda
    o menor e o maior valor de cada bloco, entao procurar "PADARIA DO" abre
    dois ou tres blocos em vez do arquivo inteiro.

    Guarda so a chave e o CNPJ. O resto dos dados vem depois da base
    principal, buscando pelo CNPJ -- que e instantaneo, porque o balde da
    particao e o ultimo digito do cnpj_basico. Carregar os dados aqui
    tambem so faria o indice inchar.

    Nao substitui a base em nada: quem procura por trecho no meio do nome
    continua varrendo a base principal, que para esse caso e menor que o
    indice. O indice acelera busca exata e "comeca com", e mais nada.
    """
    alvo = entrada / "indice"
    provisorio = entrada / "indice.montando"
    fonte = (destino / "**" / "*.parquet").as_posix()
    leitura = (f"read_parquet('{fonte}', hive_partitioning=1, "
               f"hive_types={hive_tipos_sql()})")

    # Um arquivo por tipo, e nao os tres juntos. A primeira versao ordenava
    # ~150 milhoes de chaves de uma vez e ficava dezenas de minutos sem dizer
    # nada -- de fora era indistinguivel de travado. Tres ordenacoes menores
    # terminam antes, cada uma reporta ao acabar, e a consulta so abre o
    # arquivo do tipo que interessa.
    # A UF entra junto com o CNPJ de proposito. A base e particionada por
    # (balde, uf); sabendo os dois, a busca dos dados completos abre so as
    # pastas certas. Sem a UF, 26 resultados espalhados obrigavam a abrir
    # quase todas as 280 particoes e a consulta levava 21 segundos depois de
    # o indice ja ter respondido em milissegundos.
    partes = [
        ("R", "razao social",
         f"SELECT upper(strip_accents(razao_social)) AS chave, cnpj_numerico, uf "
         f"FROM {leitura} WHERE razao_social IS NOT NULL AND razao_social <> ''"),
        ("F", "nome fantasia",
         f"SELECT upper(strip_accents(nome_fantasia)) AS chave, cnpj_numerico, uf "
         f"FROM {leitura} WHERE nome_fantasia IS NOT NULL AND nome_fantasia <> ''"),
        # A chave e o NUMERO sozinho, com o DDD numa coluna ao lado -- e nao
        # os dois grudados. O DDD e o campo mais podre da base: medido em 2
        # milhoes de registros, ~10% tem tamanho invalido (1, 3 ou 4 digitos).
        # Grudado, um DDD errado tornava o telefone impossivel de achar mesmo
        # com o numero inteiro em maos. Separado, da para procurar pelo numero
        # e usar o DDD so para ordenar quem e mais provavel.
        #
        # So digitos dos dois lados: o campo as vezes vem com espaco ou
        # tracinho, e a chave precisa ficar igual ao que a tela produz.
        ("T", "telefone",
         f"SELECT regexp_replace(telefone1, '[^0-9]', '', 'g') AS chave, "
         f"       regexp_replace(ddd1, '[^0-9]', '', 'g') AS ddd, "
         f"       cnpj_numerico, uf FROM {leitura} "
         f"WHERE telefone1 IS NOT NULL AND trim(telefone1) <> '' "
         f"UNION ALL "
         f"SELECT regexp_replace(telefone2, '[^0-9]', '', 'g'), "
         f"       regexp_replace(ddd2, '[^0-9]', '', 'g'), "
         f"       cnpj_numerico, uf FROM {leitura} "
         f"WHERE telefone2 IS NOT NULL AND trim(telefone2) <> ''"),
    ]

    # Monta num nome provisorio e so renomeia no fim. Interrupcao no meio
    # deixa lixo, nunca um indice pela metade em uso -- e indice incompleto
    # nao da erro, so acha menos empresas do que existem, calado.
    shutil.rmtree(provisorio, ignore_errors=True)
    provisorio.mkdir(parents=True)

    total = 0
    for i, (tipo, rotulo, sql) in enumerate(partes):
        if progresso:
            progresso(i, rotulo, len(partes))
        t0 = time.time()
        pasta = provisorio / f"tipo={tipo}"
        pasta.mkdir()
        con.execute(
            f"COPY ({sql} ORDER BY chave) TO '{(pasta / 'dados.parquet').as_posix()}' "
            f"(FORMAT PARQUET, COMPRESSION {COMPRESSAO}, ROW_GROUP_SIZE 200000)"
        )
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{(pasta / 'dados.parquet').as_posix()}')"
        ).fetchone()[0]
        total += n
        _log(f"    indice {rotulo}: {n:,} chaves em {time.time()-t0:.0f}s")

    shutil.rmtree(alvo, ignore_errors=True)
    provisorio.rename(alvo)
    if progresso:
        progresso(len(partes), "concluido", len(partes))
    _log(f"  indice de consulta: {total:,} chaves ({_fmt(_tamanho(alvo))})")
    return total


def gerar_indice_cidades(con, entrada, destino):
    """cidades.parquet: uma linha por (UF, municipio) que existe no dado.

    Nao e enfeite: sem ele, buscar "Assis" sem informar a UF obrigaria o
    DuckDB a abrir as 27 UFs para descobrir onde Assis fica. Com ele, a UF
    sai de um arquivo de ~200 KB e so as pastas certas sao abertas.
    """
    alvo = entrada / "cidades.parquet"
    fonte = (destino / "**" / "*.parquet").as_posix()
    con.execute(
        f"""COPY (
                SELECT uf, municipio_codigo,
                       any_value(municipio) AS municipio,
                       any_value(municipio_norm) AS municipio_norm,
                       count(*) AS qtd_empresas
                FROM read_parquet('{fonte}', hive_partitioning=1,
                                  hive_types={hive_tipos_sql()})
                WHERE municipio IS NOT NULL AND municipio <> ''
                GROUP BY uf, municipio_codigo
                ORDER BY uf, municipio
            ) TO '{alvo.as_posix()}' (FORMAT PARQUET, COMPRESSION {COMPRESSAO})"""
    )
    n, total = con.execute(
        f"SELECT count(*), sum(qtd_empresas) FROM read_parquet('{alvo.as_posix()}')"
    ).fetchone()
    _log(f"  indice de cidades: {n:,} municipios ({_fmt(_tamanho(alvo))})")
    return int(total or 0)


def validar(con, entrada, destino):
    """Confere o resultado antes de deixar a troca acontecer.

    A troca mensal substitui a base que 15 pessoas usam. Se a Receita
    publicar um arquivo truncado, e melhor abortar e continuar com a base do
    mes passado do que colocar meia base no ar sem ninguem notar.
    """
    fonte = (destino / "**" / "*.parquet").as_posix()
    linhas, ufs, com_razao = con.execute(
        f"""SELECT count(*), count(DISTINCT uf),
                   count(*) FILTER (WHERE razao_social IS NOT NULL AND razao_social <> '')
            FROM read_parquet('{fonte}', hive_partitioning=1,
                              hive_types={hive_tipos_sql()})"""
    ).fetchone()

    problemas = []
    if linhas < 1_000_000:
        problemas.append(f"so {linhas:,} linhas no total (esperado dezenas de milhoes)")
    if ufs < 20:
        problemas.append(f"so {ufs} UFs distintas (esperado 27)")
    if linhas and com_razao / linhas < 0.90:
        problemas.append(
            f"apenas {com_razao/linhas:.1%} das linhas tem razao social "
            f"-- a juncao com Empresas provavelmente falhou"
        )

    if problemas:
        raise SystemExit(
            "[erro] validacao falhou, NAO vou promover esta base:\n  - "
            + "\n  - ".join(problemas)
        )
    _log(f"  validacao ok: {linhas:,} linhas, {ufs} UFs, "
         f"{com_razao/linhas:.1%} com razao social")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--entrada", default=str(config.DIR_NOVO))
    ap.add_argument("--ufs", help="so estes estados, ex.: SP,PR")
    ap.add_argument("--so-indice", action="store_true",
                    help="regenera apenas o indice de consulta, com teto de "
                         "memoria baixo para nao atrapalhar o site no ar")
    args = ap.parse_args()

    if args.so_indice:
        entrada = Path(args.entrada)
        con = conectar_manutencao(entrada)
        try:
            gerar_indice_consulta(con, entrada, entrada / "empresas_final")
        finally:
            con.close()
        return

    consolidar(args.entrada, args.ufs.split(",") if args.ufs else None)


if __name__ == "__main__":
    main()
