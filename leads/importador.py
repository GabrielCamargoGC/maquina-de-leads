#!/usr/bin/env python3
r"""
Converte os zips da Receita em Parquet.

Por que Parquet: o formato de origem (CSV dentro de zip) obriga a
descomprimir tudo para achar qualquer coisa -- e por isso que a busca antiga
levava minutos. Parquet guarda coluna por coluna, entao uma busca em
Assis-SP le dezenas de MB em vez de 7,3 GB.

Por que os tres arquivos grandes saem particionados por BALDE de cnpj_basico
(ver coluna_balde): a etapa de juncao precisa cruzar 72 M de estabelecimentos
com 69 M de empresas. Fazer isso de uma vez estoura os 8 GB do desktop.
Pre-dividindo os dois lados pelo mesmo criterio, a juncao vira 10 juncoes
pequenas -- cada uma cabe folgado em memoria, e cada arquivo continua sendo
lido uma unica vez.

Fluxo:
    1. Municipios/Cnaes  -> parquet de consulta (KB)
    2. Simples           -> parquet por balde
    3. Empresas*         -> parquet por balde
    4. Estabelecimentos* -> parquet por balde

A juncao acontece na etapa 5 (leads/consolidar.py).

Uso:
    python -m leads.importador                 # tudo
    python -m leads.importador --etapa 4       # so estabelecimentos
    python -m leads.importador --ufs SP,PR     # so alguns estados (teste)
"""
import argparse
import glob
import shutil
import sys
import time
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from pyarrow import csv as pacsv

from . import config, layout

COMPRESSAO = "zstd"

PARTICAO_BALDE = ds.partitioning(pa.schema([("balde", pa.string())]), flavor="hive")


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _tamanho(caminho):
    p = Path(caminho)
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _fmt(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def coluna_balde(tabela):
    """Acrescenta 'balde' = ultimo digito do cnpj_basico.

    O ultimo digito serve de balde porque o CNPJ e atribuido em sequencia:
    os digitos finais ficam distribuidos por igual, entao os 10 baldes saem
    do mesmo tamanho sem precisar calcular hash. E, sendo derivado so do
    cnpj_basico, o mesmo CNPJ cai sempre no mesmo balde nos tres arquivos --
    que e exatamente o que permite juntar balde por balde.
    """
    b = pc.utf8_slice_codeunits(tabela["cnpj_basico"], 7, 8)
    # cnpj_basico torto (vazio ou curto) cairia num balde de nome vazio;
    # manda para o balde 0 em vez de criar uma particao invalida.
    b = pc.if_else(pc.equal(pc.binary_length(b), 0), pa.scalar("0"), b)
    return tabela.append_column("balde", b)


def _opcoes_leitura(colunas):
    """CSV da Receita: sem cabecalho, ponto-e-virgula, aspas duplas, latin-1.

    Tudo entra como texto. Converter tipo aqui faria o arquivo inteiro
    abortar num unico campo torto -- e dado real da Receita tem campo torto.
    A conversao fica na consolidacao, onde da para tratar campo a campo.
    """
    return (
        pacsv.ReadOptions(
            column_names=colunas,
            encoding="latin-1",
            block_size=config.BLOCO_CSV,
        ),
        pacsv.ParseOptions(delimiter=";", quote_char='"', newlines_in_values=False),
        pacsv.ConvertOptions(column_types={c: pa.string() for c in colunas}),
    )


def _ler_zip_em_lotes(caminho_zip, colunas):
    """Gera RecordBatch lendo o CSV de dentro do zip em fluxo -- nunca
    descompacta para o disco."""
    ro, po, co = _opcoes_leitura(colunas)
    with zipfile.ZipFile(caminho_zip) as z:
        for nome in z.namelist():
            with z.open(nome) as f:
                leitor = pacsv.open_csv(
                    pa.PythonFile(f),
                    read_options=ro,
                    parse_options=po,
                    convert_options=co,
                )
                for lote in leitor:
                    yield lote


def _zips(dir_downloads, prefixo):
    return sorted(glob.glob(str(Path(dir_downloads) / (prefixo + "*.zip"))))


def _gravar_por_balde(tabela, destino, nome_base):
    ds.write_dataset(
        tabela,
        destino,
        format="parquet",
        partitioning=PARTICAO_BALDE,
        basename_template=nome_base,
        existing_data_behavior="overwrite_or_ignore",
        file_options=ds.ParquetFileFormat().make_write_options(compression=COMPRESSAO),
    )


def _preparar(destino):
    destino = Path(destino)
    if destino.exists():
        shutil.rmtree(destino)
    destino.mkdir(parents=True)
    return destino


# ---------------------------------------------------------------- etapas


def etapa_lookups(dir_downloads, dir_saida, _ufs=None):
    """Municipios.zip e Cnaes.zip -> parquet. Sao KB, cabem em memoria."""
    for nome in ("Municipios", "Cnaes"):
        origem = Path(dir_downloads) / f"{nome}.zip"
        if not origem.exists():
            _log(f"  [aviso] {origem.name} nao encontrado, pulando")
            continue
        lotes = list(_ler_zip_em_lotes(origem, layout.LOOKUP_COLUNAS))
        tabela = pa.Table.from_batches(lotes)
        destino = Path(dir_saida) / f"{nome.lower()}.parquet"
        pq.write_table(tabela, destino, compression=COMPRESSAO)
        _log(f"  {nome}: {tabela.num_rows:,} linhas -> {destino.name}")


def etapa_simples(dir_downloads, dir_saida, _ufs=None):
    """Simples.zip -> parquet por balde, so com o que a busca usa."""
    origem = Path(dir_downloads) / "Simples.zip"
    if not origem.exists():
        _log("  [aviso] Simples.zip nao encontrado, pulando")
        return
    destino = _preparar(Path(dir_saida) / "simples")
    manter = ["cnpj_basico", "opcao_simples", "opcao_mei"]
    total = 0
    t0 = time.time()
    for j, lote in enumerate(_ler_zip_em_lotes(origem, layout.SIMPLES_COLUNAS)):
        tabela = coluna_balde(pa.Table.from_batches([lote]).select(manter))
        _gravar_por_balde(tabela, destino, f"s-{j}-{{i}}.parquet")
        total += tabela.num_rows
    _log(f"  Simples: {total:,} linhas em {time.time()-t0:.0f}s -> {_fmt(_tamanho(destino))}")


def etapa_empresas(dir_downloads, dir_saida, _ufs=None):
    """Empresas*.zip -> parquet por balde (razao social, porte, capital)."""
    origens = _zips(dir_downloads, "Empresas")
    if not origens:
        _log("  [aviso] nenhum Empresas*.zip, pulando")
        return
    destino = _preparar(Path(dir_saida) / "empresas")
    manter = ["cnpj_basico", "razao_social", "natureza_juridica",
              "capital_social", "porte_empresa"]
    total = 0
    t0 = time.time()
    for idx, origem in enumerate(origens):
        n_arq = 0
        for j, lote in enumerate(_ler_zip_em_lotes(origem, layout.EMP_COLUNAS)):
            tabela = coluna_balde(pa.Table.from_batches([lote]).select(manter))
            _gravar_por_balde(tabela, destino, f"e-{idx}-{j}-{{i}}.parquet")
            n_arq += tabela.num_rows
        total += n_arq
        _log(f"    {Path(origem).name}: {n_arq:,} (total {total:,})")
    _log(f"  Empresas: {total:,} linhas em {time.time()-t0:.0f}s -> {_fmt(_tamanho(destino))}")


def etapa_estabelecimentos(dir_downloads, dir_saida, ufs=None):
    """Estabelecimentos*.zip -> parquet por balde de cnpj_basico.

    Nao particiona por UF aqui de proposito: a UF so importa no arquivo
    final, e particionar por ela nesta etapa impediria a juncao por balde
    (que e o que faz a consolidacao caber em 8 GB).
    """
    origens = _zips(dir_downloads, "Estabelecimentos")
    if not origens:
        sys.exit(f"[erro] nenhum Estabelecimentos*.zip em {dir_downloads}")

    destino = _preparar(Path(dir_saida) / "estabelecimentos")
    filtro_ufs = pa.array(sorted({u.strip().upper() for u in ufs})) if ufs else None
    total = 0
    t0 = time.time()

    for idx, origem in enumerate(origens):
        n_arq = 0
        t_arq = time.time()
        for j, lote in enumerate(_ler_zip_em_lotes(origem, layout.EST_COLUNAS)):
            tabela = pa.Table.from_batches([lote]).select(layout.EST_COLUNAS_MANTIDAS)
            if filtro_ufs is not None:
                tabela = tabela.filter(pc.is_in(tabela["uf"], value_set=filtro_ufs))
                if tabela.num_rows == 0:
                    continue
            _gravar_por_balde(coluna_balde(tabela), destino, f"x-{idx}-{j}-{{i}}.parquet")
            n_arq += tabela.num_rows
        total += n_arq
        _log(f"    {Path(origem).name}: {n_arq:,} em {time.time()-t_arq:.0f}s (total {total:,})")

    _log(f"  Estabelecimentos: {total:,} linhas em {time.time()-t0:.0f}s "
         f"-> {_fmt(_tamanho(destino))}")
    return total


ETAPAS = {
    1: ("Municipios/Cnaes", etapa_lookups),
    2: ("Simples", etapa_simples),
    3: ("Empresas", etapa_empresas),
    4: ("Estabelecimentos", etapa_estabelecimentos),
}


def importar(dir_downloads, dir_saida, ufs=None, etapa=None):
    saida = Path(dir_saida)
    saida.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    for num in sorted(ETAPAS):
        if etapa and etapa != num:
            continue
        nome, fn = ETAPAS[num]
        _log(f"Etapa {num}/4 - {nome}")
        fn(dir_downloads, saida, ufs)
    _log(f"Importacao concluida em {time.time()-t0:.0f}s. "
         f"Saida: {saida} ({_fmt(_tamanho(saida))})")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--downloads", default=str(config.DIR_DOWNLOADS))
    ap.add_argument("--saida", default=str(config.DIR_NOVO))
    ap.add_argument("--ufs", help="teste: so estes estados, ex.: SP,PR")
    ap.add_argument("--etapa", type=int, choices=sorted(ETAPAS),
                    help="roda so uma etapa (1=lookups 2=simples 3=empresas 4=estabelecimentos)")
    args = ap.parse_args()
    importar(args.downloads, args.saida,
             args.ufs.split(",") if args.ufs else None, args.etapa)


if __name__ == "__main__":
    main()
