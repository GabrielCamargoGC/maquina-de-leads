#!/usr/bin/env python3
r"""
Export de planilha (CSV ou Excel) como tarefa em segundo plano.

Por que nao gerar direto na resposta do site: uma cidade grande sem filtro
passa de 200 mil linhas. Montar isso dentro do request estoura o tempo do
navegador e do tunel, e trava uma thread do servidor por minutos. Aqui o
pedido entra numa fila, o navegador recebe um numero, e a pagina avisa
quando o arquivo esta pronto.

Escreve em fluxo (lote a lote vindo do DuckDB) -- nunca monta a planilha
inteira na memoria, o que importa num desktop de 8 GB dividido com 15
pessoas.
"""
import csv
import json
import os
import queue
import sqlite3
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from . import busca, config, novidades

# Quanto tempo a planilha exportada fica no disco antes de sair.
DIAS_GUARDAR = int(os.environ.get("LEADS_DIAS_EXPORT", "15"))

# Intervalo da faxina automatica.
HORAS_FAXINA = 24

# Cabecalho da planilha em portugues -- quem recebe o arquivo nao precisa
# saber o nome tecnico da coluna.
ROTULOS = {
    "cnpj": "CNPJ",
    "razao_social": "Razao Social",
    "nome_fantasia": "Nome Fantasia",
    "situacao_desc": "Situacao",
    "data_situacao": "Situacao Desde",
    "cnae_principal": "CNAE",
    "cnae_descricao": "Ramo (CNAE)",
    "porte_desc": "Porte",
    "optante_simples": "Simples Nacional",
    "optante_mei": "MEI",
    "logradouro": "Logradouro",
    "numero": "Numero",
    "complemento": "Complemento",
    "bairro": "Bairro",
    "cep": "CEP",
    "municipio": "Municipio",
    "uf": "UF",
    "telefone_fmt": "Telefone",
    "ddd1": "DDD 1",
    "telefone1": "Telefone 1",
    "ddd2": "DDD 2",
    "telefone2": "Telefone 2",
    "email": "E-mail",
    "capital_social": "Capital Social",
    "natureza_juridica": "Natureza Juridica",
    "data_abertura": "Data de Abertura",
    "matriz_filial_desc": "Matriz/Filial",
}

COLUNAS_EXPORT = list(ROTULOS)


def _valor(v):
    """Booleano vira Sim/Nao e data vira dd/mm/aaaa -- e planilha para
    pessoa ler, nao para maquina reprocessar."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Sim" if v else "Nao"
    if hasattr(v, "strftime"):
        return v.strftime("%d/%m/%Y")
    return v


# ------------------------------------------------------------ banco da fila


def _conectar_banco():
    con = sqlite3.connect(config.BANCO_APP, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")  # 15 pessoas lendo enquanto grava
    con.execute("PRAGMA busy_timeout=30000")
    return con


def criar_tabelas():
    config.BANCO_APP.parent.mkdir(parents=True, exist_ok=True)
    con = _conectar_banco()
    con.execute(
        """CREATE TABLE IF NOT EXISTS export_job (
               id TEXT PRIMARY KEY,
               criado_em TEXT NOT NULL,
               concluido_em TEXT,
               formato TEXT NOT NULL,
               descricao TEXT,
               filtros TEXT NOT NULL,
               estado TEXT NOT NULL,
               linhas INTEGER,
               arquivo TEXT,
               erro TEXT
           )"""
    )
    con.commit()
    con.close()


def _gravar(sql, params=()):
    con = _conectar_banco()
    con.execute(sql, params)
    con.commit()
    con.close()


def ver_job(job_id):
    con = _conectar_banco()
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM export_job WHERE id = ?", (job_id,)).fetchone()
    con.close()
    return dict(r) if r else None


def listar_jobs(limite=20):
    con = _conectar_banco()
    con.row_factory = sqlite3.Row
    r = con.execute(
        "SELECT * FROM export_job ORDER BY criado_em DESC LIMIT ?", (limite,)
    ).fetchall()
    con.close()
    return [dict(x) for x in r]


# ------------------------------------------------------------ escrita


def _leitor(filtros, fonte, dir_dados):
    """A tela de busca e a de novidades exportam as mesmas colunas, so muda
    de onde as linhas vem."""
    if fonte == "novidades":
        return novidades.arrow_novas(filtros, COLUNAS_EXPORT, dir_atual=dir_dados)
    return busca.buscar_arrow(filtros, colunas=COLUNAS_EXPORT, dir_dados=dir_dados)


def escrever_csv(filtros, destino, dir_dados=None, fonte="busca"):
    """CSV com BOM e ';' -- e o que o Excel em portugues abre com as colunas
    separadas ao dar duplo clique. Sem isso tudo cai numa coluna so."""
    total = 0
    leitor = _leitor(filtros, fonte, dir_dados)
    with open(destino, "w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.writer(f, delimiter=";")
        escritor.writerow(ROTULOS.values())
        for lote in leitor:
            for linha in lote.to_pylist():
                escritor.writerow([_valor(linha[c]) for c in COLUNAS_EXPORT])
                total += 1
    return total


def escrever_xlsx(filtros, destino, dir_dados=None, fonte="busca"):
    """Excel em modo constant_memory: o xlsxwriter grava linha a linha no
    disco em vez de segurar a planilha inteira na RAM."""
    import xlsxwriter

    total = 0
    livro = xlsxwriter.Workbook(
        str(destino), {"constant_memory": True, "default_date_format": "dd/mm/yyyy"}
    )
    aba = livro.add_worksheet("Leads")
    negrito = livro.add_format({"bold": True, "bg_color": "#EEF1FD", "border": 1})

    for i, rotulo in enumerate(ROTULOS.values()):
        aba.write(0, i, rotulo, negrito)
    aba.freeze_panes(1, 0)
    aba.autofilter(0, 0, 0, len(COLUNAS_EXPORT) - 1)
    aba.set_column(0, 0, 20)
    aba.set_column(1, 2, 38)
    aba.set_column(5, 5, 42)

    leitor = _leitor(filtros, fonte, dir_dados)
    linha_n = 1
    for lote in leitor:
        for linha in lote.to_pylist():
            for i, c in enumerate(COLUNAS_EXPORT):
                aba.write(linha_n, i, _valor(linha[c]))
            linha_n += 1
            total += 1
    livro.close()
    return total


# ------------------------------------------------------------ fila


_fila = queue.Queue()
_trabalhadores = []
_iniciado = threading.Lock()


def _processar(job_id, filtros, formato, dir_dados, fonte):
    destino = config.DIR_EXPORTS / f"{job_id}.{formato}"
    try:
        _gravar("UPDATE export_job SET estado='rodando' WHERE id=?", (job_id,))
        escrever = escrever_xlsx if formato == "xlsx" else escrever_csv
        total = escrever(filtros, destino, dir_dados, fonte)
        _gravar(
            "UPDATE export_job SET estado='pronto', linhas=?, arquivo=?, concluido_em=? "
            "WHERE id=?",
            (total, str(destino), datetime.now().isoformat(timespec="seconds"), job_id),
        )
    except busca.ErroBusca as e:
        _gravar(
            "UPDATE export_job SET estado='erro', erro=?, concluido_em=? WHERE id=?",
            (str(e), datetime.now().isoformat(timespec="seconds"), job_id),
        )
    except Exception as e:
        traceback.print_exc()
        _gravar(
            "UPDATE export_job SET estado='erro', erro=?, concluido_em=? WHERE id=?",
            (f"Falha inesperada: {e}", datetime.now().isoformat(timespec="seconds"), job_id),
        )


def _laco():
    while True:
        item = _fila.get()
        if item is None:
            return
        try:
            _processar(*item)
        finally:
            _fila.task_done()


def iniciar_workers():
    """Poucos workers de proposito: dois exports pesados ao mesmo tempo ja
    ocupam o disco e a memoria que as buscas interativas precisam. O resto
    espera na fila em vez de deixar o site lento para todo mundo."""
    with _iniciado:
        if _trabalhadores:
            return
        criar_tabelas()
        config.DIR_EXPORTS.mkdir(parents=True, exist_ok=True)
        for _ in range(config.EXPORTS_SIMULTANEOS):
            t = threading.Thread(target=_laco, daemon=True)
            t.start()
            _trabalhadores.append(t)


_faxina = []


def iniciar_faxina(dias=None):
    """Apaga planilha velha uma vez por dia, de dentro do site.

    Antes isto so acontecia no passo 5/5 do job de atualizacao -- que sai
    logo no comeco quando a Receita nao publicou base nova. Ou seja: a
    limpeza de 15 dias acontecia de fato uma vez por mes, e no mes inteiro
    o disco so crescia.

    Thread de fundo e nao tarefa agendada do Windows: o servico ja fica de
    pe o tempo todo, e uma coisa a menos para instalar e lembrar.
    """
    dias = DIAS_GUARDAR if dias is None else dias

    with _iniciado:
        if _faxina:
            return

        def laco():
            # Espera um pouco antes da primeira passada: subir o site e o
            # que importa no instante do boot.
            time.sleep(90)
            while True:
                try:
                    n = limpar_antigos(dias)
                    if n:
                        print(f"[faxina] {n} planilha(s) com mais de {dias} "
                              f"dias removida(s)", flush=True)
                except Exception:
                    # Faxina que falha nao pode derrubar o site nem a propria
                    # thread: erra hoje, tenta de novo amanha.
                    traceback.print_exc()
                time.sleep(HORAS_FAXINA * 3600)

        t = threading.Thread(target=laco, daemon=True, name="faxina-exports")
        t.start()
        _faxina.append(t)


def enfileirar(filtros, formato="csv", descricao="", dir_dados=None, fonte="busca"):
    iniciar_workers()
    if formato not in ("csv", "xlsx"):
        raise ValueError("formato deve ser csv ou xlsx")
    job_id = uuid.uuid4().hex[:12]
    _gravar(
        "INSERT INTO export_job (id, criado_em, formato, descricao, filtros, estado) "
        "VALUES (?,?,?,?,?, 'na_fila')",
        (
            job_id,
            datetime.now().isoformat(timespec="seconds"),
            formato,
            descricao,
            json.dumps(filtros.__dict__, default=str, ensure_ascii=False),

        ),
    )
    _fila.put((job_id, filtros, formato, dir_dados, fonte))
    return job_id


def limpar_antigos(dias=DIAS_GUARDAR):
    """Export e descartavel: quem precisa de novo refaz em 2 segundos.
    Guardar semanas de planilha so enche o disco do desktop.

    Cada arquivo tem o proprio try. No Windows, planilha que esta sendo
    baixada naquele instante fica travada e o unlink estoura -- sem esta
    protecao o erro subia e o laco parava no meio, deixando sem limpar tudo
    que vinha depois. O arquivo travado hoje sai na faxina de amanha.
    """
    corte = time.time() - dias * 86400
    removidos = 0
    for f in config.DIR_EXPORTS.glob("*.*"):
        try:
            if f.stat().st_mtime < corte:
                f.unlink()
                removidos += 1
        except OSError:
            continue
    # A limpeza do banco vai em try separado de proposito: quem enche o disco
    # sao os arquivos, e uma falha ao podar o historico nao pode anular o
    # trabalho que ja foi feito la em cima.
    try:
        con = _conectar_banco()
        try:
            con.execute(
                "DELETE FROM export_job WHERE criado_em < datetime('now', ?)",
                (f"-{dias} days",),
            )
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return removidos
