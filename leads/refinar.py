#!/usr/bin/env python3
r"""
Refinar: filtra uma planilha que ja saiu deste sistema.

A pessoa exportou 5 mil contatos de uma cidade e agora quer so os que sao
micro empresa, fora do Simples e fora do MEI. Sem isto, ela abriria o Excel e
montaria filtro a mao -- que funciona, mas se perde na proxima vez e nao deixa
registro de como a lista foi feita.

Nao consulta a base. A planilha exportada JA traz porte, Simples, MEI,
situacao, endereco e contato -- filtrar o arquivo e comparar texto, nao
procurar 5 mil CNPJs em 72 milhoes de linhas. Por isso responde em menos de
um segundo mesmo com 100 mil linhas.

So aceita planilha deste sistema. O cabecalho e conferido contra os rotulos
que a exportacao escreve; arquivo de outra origem e recusado com o motivo,
em vez de adivinhar qual coluna seria qual e devolver lista errada em
silencio.
"""
import re
import unicodedata
import uuid
from pathlib import Path

import duckdb

from . import config, exportar

LIMITE_LINHAS = 200_000
LIMITE_PREVIA = 50

# rotulo na planilha -> nome interno da coluna
POR_ROTULO = {rotulo: interno for interno, rotulo in exportar.ROTULOS.items()}

# Colunas sem as quais nao da para afirmar que o arquivo veio daqui.
OBRIGATORIAS = {"CNPJ", "Razao Social"}


class ErroPlanilha(Exception):
    """Problema que a pessoa consegue corrigir -- arquivo errado, vazio,
    grande demais. A tela mostra a mensagem."""


def _achatar(texto):
    t = unicodedata.normalize("NFKD", str(texto or "").strip().upper())
    return "".join(c for c in t if not unicodedata.combining(c))


def _conectar():
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{config.DUCKDB_MEMORIA}'")
    con.execute("SET threads=2")
    return con


def _fonte(caminho):
    """Trecho FROM para ler a planilha enviada.

    xlsx sai pela extensao 'excel' do proprio DuckDB, e nao por biblioteca
    Python: mantem a promessa de nao acrescentar dependencia. A extensao e
    baixada uma vez e fica em cache.
    """
    p = Path(caminho)
    if p.suffix.lower() == ".xlsx":
        return f"read_xlsx('{p.as_posix()}', all_varchar = true)"
    # CSV nosso: ';' e UTF-8 com BOM. all_varchar para nao adivinhar tipo e
    # transformar CNPJ com zero a esquerda em numero.
    return (f"read_csv('{p.as_posix()}', delim = ';', header = true, "
            f"all_varchar = true, ignore_errors = true)")


def inspecionar(caminho):
    """Le so o cabecalho e a contagem. Devolve o que a tela precisa para
    montar os filtros que aquele arquivo comporta."""
    con = _conectar()
    try:
        if Path(caminho).suffix.lower() == ".xlsx":
            con.execute("INSTALL excel")
            con.execute("LOAD excel")
        fonte = _fonte(caminho)
        try:
            colunas = [d[0] for d in con.execute(f"SELECT * FROM {fonte} LIMIT 0").description]
        except Exception as e:
            raise ErroPlanilha(
                "Nao consegui ler este arquivo. Envie o .xlsx ou .csv como "
                f"saiu da aba Planilhas.\n\n{str(e)[:200]}"
            )

        presentes = {c.strip() for c in colunas}
        if not OBRIGATORIAS.issubset(presentes):
            faltando = ", ".join(sorted(OBRIGATORIAS - presentes))
            raise ErroPlanilha(
                f"Esta planilha nao parece ter saido daqui: falta a coluna "
                f"{faltando}. Use um arquivo baixado na aba Planilhas."
            )

        linhas = con.execute(f"SELECT count(*) FROM {fonte}").fetchone()[0]
        if linhas == 0:
            raise ErroPlanilha("A planilha esta vazia.")
        if linhas > LIMITE_LINHAS:
            raise ErroPlanilha(
                f"A planilha tem {linhas:,} linhas e o limite e "
                f"{LIMITE_LINHAS:,}.".replace(",", ".")
            )

        internas = {POR_ROTULO[c] for c in presentes if c in POR_ROTULO}
        return {"linhas": linhas, "colunas": sorted(presentes),
                "internas": internas}
    finally:
        con.close()


# ------------------------------------------------------------ filtros


def _texto(col, valor):
    return f"upper(strip_accents(\"{col}\")) LIKE ?", [f"%{_achatar(valor)}%"]


def montar_condicoes(f, internas):
    """(lista de condicoes SQL, parametros).

    Cada filtro so entra se a coluna dele existir no arquivo. Planilha
    exportada antes de as colunas novas aparecerem continua servindo, com
    menos filtros -- melhor que recusar o arquivo inteiro.
    """
    cond, par = [], []

    def tem(interno):
        return interno in internas

    if f.get("portes") and tem("porte_desc"):
        marcas = ",".join("?" for _ in f["portes"])
        cond.append(f'"{exportar.ROTULOS["porte_desc"]}" IN ({marcas})')
        par.extend(f["portes"])

    for chave, interno in (("simples", "optante_simples"), ("mei", "optante_mei")):
        v = f.get(chave)
        if v in ("Sim", "Nao") and tem(interno):
            cond.append(f'"{exportar.ROTULOS[interno]}" = ?')
            par.append(v)

    if f.get("situacoes") and tem("situacao_desc"):
        marcas = ",".join("?" for _ in f["situacoes"])
        cond.append(f'"{exportar.ROTULOS["situacao_desc"]}" IN ({marcas})')
        par.extend(f["situacoes"])

    if f.get("com_telefone") and tem("telefone_fmt"):
        cond.append(f'coalesce("{exportar.ROTULOS["telefone_fmt"]}", \'\') <> \'\'')
    if f.get("com_email") and tem("email"):
        cond.append(f'coalesce("{exportar.ROTULOS["email"]}", \'\') <> \'\'')

    if f.get("so_matriz") and tem("matriz_filial_desc"):
        cond.append(f'"{exportar.ROTULOS["matriz_filial_desc"]}" = \'Matriz\'')

    if f.get("ufs") and tem("uf"):
        marcas = ",".join("?" for _ in f["ufs"])
        cond.append(f'upper("{exportar.ROTULOS["uf"]}") IN ({marcas})')
        par.extend(u.upper() for u in f["ufs"])

    for chave, interno in (("cidade", "municipio"), ("bairro", "bairro"),
                           ("natureza", "natureza_juridica")):
        if f.get(chave) and tem(interno):
            sql, p = _texto(exportar.ROTULOS[interno], f[chave])
            cond.append(sql)
            par.extend(p)

    if f.get("cnae") and tem("cnae_principal"):
        alvo = str(f["cnae"]).strip()
        if alvo.replace("-", "").replace("/", "").isdigit():
            cond.append(f'"{exportar.ROTULOS["cnae_principal"]}" LIKE ?')
            par.append(alvo.replace("-", "").replace("/", "") + "%")
        elif tem("cnae_descricao"):
            sql, p = _texto(exportar.ROTULOS["cnae_descricao"], alvo)
            cond.append(sql)
            par.extend(p)

    if f.get("capital_min") is not None and tem("capital_social"):
        # TRY_CAST porque a coluna chega como texto e o valor pode vir vazio
        cond.append(f'TRY_CAST("{exportar.ROTULOS["capital_social"]}" AS DOUBLE) >= ?')
        par.append(float(f["capital_min"]))

    # Datas saem da exportacao no formato brasileiro; comparar como texto
    # ordenaria por dia. strptime traz de volta para data de verdade.
    for chave_de, chave_ate, interno in (
            ("aberta_de", "aberta_ate", "data_abertura"),
            ("situacao_de", "situacao_ate", "data_situacao")):
        col = exportar.ROTULOS.get(interno)
        if not tem(interno):
            continue
        if f.get(chave_de):
            cond.append(f"try_strptime(\"{col}\", '%d/%m/%Y') >= ?")
            par.append(f[chave_de])
        if f.get(chave_ate):
            cond.append(f"try_strptime(\"{col}\", '%d/%m/%Y') <= ?")
            par.append(f[chave_ate])

    return cond, par


def refinar(caminho, filtros, internas):
    """Aplica os filtros e guarda o resultado. Devolve o resumo para a tela.

    O resultado vai para parquet, e nao direto para xlsx: ocupa pouco, sai
    rapido, e permite gerar Excel ou CSV depois sem pedir a planilha de novo.
    """
    con = _conectar()
    try:
        if Path(caminho).suffix.lower() == ".xlsx":
            con.execute("INSTALL excel")
            con.execute("LOAD excel")
        fonte = _fonte(caminho)
        cond, par = montar_condicoes(filtros, internas)
        onde = (" WHERE " + " AND ".join(cond)) if cond else ""

        total = con.execute(f"SELECT count(*) FROM {fonte}").fetchone()[0]

        ident = uuid.uuid4().hex[:12]
        config.DIR_EXPORTS.mkdir(parents=True, exist_ok=True)
        destino = config.DIR_EXPORTS / f"refinado-{ident}.parquet"
        con.execute(
            f"COPY (SELECT * FROM {fonte}{onde}) TO '{destino.as_posix()}' "
            f"(FORMAT PARQUET)", par
        )
        sobraram = con.execute(
            f"SELECT count(*) FROM read_parquet('{destino.as_posix()}')"
        ).fetchone()[0]

        previa = []
        if sobraram:
            rel = con.execute(
                f"SELECT * FROM read_parquet('{destino.as_posix()}') "
                f"LIMIT {LIMITE_PREVIA}"
            )
            nomes = [d[0] for d in rel.description]
            previa = [dict(zip(nomes, l)) for l in rel.fetchall()]

        return {"id": ident, "total": total, "sobraram": sobraram,
                "previa": previa, "condicoes": len(cond)}
    finally:
        con.close()


def caminho_resultado(ident):
    """Valida o identificador antes de montar caminho.

    Vem da URL: sem esta checagem, um ".." levaria a leitura para fora da
    pasta de exportacao.
    """
    if not re.fullmatch(r"[0-9a-f]{12}", ident or ""):
        return None
    p = config.DIR_EXPORTS / f"refinado-{ident}.parquet"
    return p if p.exists() else None


def escrever(ident, formato):
    """Gera o arquivo final a partir do resultado guardado."""
    origem = caminho_resultado(ident)
    if not origem:
        raise ErroPlanilha("Este resultado expirou. Refine a planilha de novo.")

    destino = config.DIR_EXPORTS / f"refinado-{ident}.{formato}"
    if destino.exists():
        return destino

    con = _conectar()
    try:
        if formato == "csv":
            # ';' e UTF-8 com BOM, igual a exportacao normal: e o que o Excel
            # em portugues abre com as colunas separadas.
            con.execute(
                f"COPY (SELECT * FROM read_parquet('{origem.as_posix()}')) "
                f"TO '{destino.as_posix()}' (FORMAT CSV, DELIMITER ';', HEADER)"
            )
            bruto = destino.read_bytes()
            if not bruto.startswith(b"\xef\xbb\xbf"):
                destino.write_bytes(b"\xef\xbb\xbf" + bruto)
        else:
            import xlsxwriter

            rel = con.execute(f"SELECT * FROM read_parquet('{origem.as_posix()}')")
            nomes = [d[0] for d in rel.description]
            livro = xlsxwriter.Workbook(str(destino), {"constant_memory": True})
            aba = livro.add_worksheet("Leads")
            negrito = livro.add_format({"bold": True, "bg_color": "#FFF2E8",
                                        "border": 1})
            for i, n in enumerate(nomes):
                aba.write(0, i, n, negrito)
            aba.freeze_panes(1, 0)
            aba.autofilter(0, 0, 0, len(nomes) - 1)
            aba.set_column(0, 0, 20)
            aba.set_column(1, 2, 38)
            linha = 1
            while True:
                lote = rel.fetchmany(5000)
                if not lote:
                    break
                for l in lote:
                    for i, v in enumerate(l):
                        aba.write(linha, i, "" if v is None else v)
                    linha += 1
            livro.close()
        return destino
    finally:
        con.close()
