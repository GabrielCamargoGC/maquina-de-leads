#!/usr/bin/env python3
r"""
Registro de eventos (SIEM) -- quem fez o que, quando e de onde.

Guarda o que importa para responder tres perguntas depois do fato:

  - alguem esta tentando entrar a forca? (erro de senha repetido, bloqueio)
  - quem mexeu nas contas? (criacao, desativacao, senha temporaria)
  - quem tirou qual lista, e quando? (exportacao, com filtros e quantidade)

A terceira nao e enfeite de seguranca: como a base tem telefone e e-mail de
empresa, saber quem exportou o que e a resposta pronta caso alguem pergunte
de onde saiu uma lista. Sai de graca junto com o resto.

Guardado no mesmo SQLite do app, com limpeza automatica: registro de acesso
que cresce para sempre vira problema de disco, nao ferramenta.
"""
import json
from datetime import datetime, timedelta, timezone

from . import config, contas

DIAS_GUARDAR = 180

# Tipos de evento. Constantes e nao texto solto para nao acabar com
# "login_falhou" e "login-falhou" convivendo na mesma tabela.
LOGIN_OK = "login_ok"
LOGIN_ERRO = "login_erro"
LOGIN_BLOQUEADO = "login_bloqueado"
SAIU = "saiu"
CONTA_CRIADA = "conta_criada"
CONTA_ATIVADA = "conta_ativada"
CONTA_DESATIVADA = "conta_desativada"
SENHA_TROCADA = "senha_trocada"
SENHA_TEMPORARIA = "senha_temporaria"
CODIGO_USADO = "codigo_recuperacao_usado"
CODIGOS_REFEITOS = "codigos_refeitos"
EXPORTOU = "exportou"
CODIGO_ATUALIZADO = "codigo_atualizado"
INDICE_REFEITO = "indice_refeito"

ROTULOS = {
    LOGIN_OK: "Entrou",
    LOGIN_ERRO: "Senha errada",
    LOGIN_BLOQUEADO: "Bloqueado por tentativas",
    SAIU: "Saiu",
    CONTA_CRIADA: "Conta criada",
    CONTA_ATIVADA: "Conta reativada",
    CONTA_DESATIVADA: "Conta desativada",
    SENHA_TROCADA: "Senha trocada",
    SENHA_TEMPORARIA: "Senha temporaria gerada",
    CODIGO_USADO: "Recuperou com codigo",
    CODIGOS_REFEITOS: "Codigos de recuperacao refeitos",
    EXPORTOU: "Exportou planilha",
    CODIGO_ATUALIZADO: "Codigo atualizado",
    INDICE_REFEITO: "Indice de consulta refeito",
}

# Eventos que merecem destaque na tela do master.
GRAVES = {LOGIN_ERRO, LOGIN_BLOQUEADO, CONTA_DESATIVADA, SENHA_TEMPORARIA,
          CODIGO_ATUALIZADO}


def criar_tabelas():
    con = contas.conectar()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS evento (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            quando   TEXT NOT NULL,
            tipo     TEXT NOT NULL,
            usuario  TEXT NOT NULL DEFAULT '',
            ip       TEXT NOT NULL DEFAULT '',
            detalhe  TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_evento_quando ON evento(quando DESC);
        CREATE INDEX IF NOT EXISTS ix_evento_tipo ON evento(tipo, quando DESC);
        """
    )
    con.commit()


def registrar(tipo, usuario="", ip="", **detalhe):
    """Grava um evento. Nunca levanta excecao.

    Falha de auditoria nao pode derrubar login nem exportacao: perder uma
    linha de registro e ruim, deixar a pessoa sem entrar por causa disso e
    pior.
    """
    try:
        con = contas.conectar()
        con.execute(
            "INSERT INTO evento (quando, tipo, usuario, ip, detalhe) VALUES (?,?,?,?,?)",
            (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                tipo, (usuario or "")[:64], (ip or "")[:45],
                json.dumps(detalhe, ensure_ascii=False, default=str)[:800] if detalhe else "",
            ),
        )
        con.commit()
    except Exception:
        pass


def listar(limite=100, tipo=None, usuario=None, so_graves=False):
    con = contas.conectar()
    sql = "SELECT * FROM evento WHERE 1=1"
    params = []
    if tipo:
        sql += " AND tipo = ?"
        params.append(tipo)
    if usuario:
        sql += " AND usuario = ?"
        params.append(usuario)
    if so_graves:
        sql += " AND tipo IN (" + ",".join("?" for _ in GRAVES) + ")"
        params.extend(sorted(GRAVES))
    sql += " ORDER BY quando DESC, id DESC LIMIT ?"
    params.append(int(limite))

    linhas = []
    for r in con.execute(sql, params).fetchall():
        d = dict(r)
        d["rotulo"] = ROTULOS.get(d["tipo"], d["tipo"])
        d["grave"] = d["tipo"] in GRAVES
        try:
            d["detalhe_dict"] = json.loads(d["detalhe"]) if d["detalhe"] else {}
        except json.JSONDecodeError:
            d["detalhe_dict"] = {}
        d["quando_local"] = _local(d["quando"])
        linhas.append(d)
    return linhas


def resumo(horas=24):
    """Contagem por tipo nas ultimas horas -- o cabecalho da tela do master."""
    desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
    con = contas.conectar()
    linhas = con.execute(
        "SELECT tipo, count(*) AS n FROM evento WHERE quando > ? GROUP BY tipo",
        (desde,),
    ).fetchall()
    return {r["tipo"]: r["n"] for r in linhas}


def limpar_antigos(dias=DIAS_GUARDAR):
    try:
        corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
        con = contas.conectar()
        cur = con.execute("DELETE FROM evento WHERE quando < ?", (corte,))
        con.commit()
        return cur.rowcount
    except Exception:
        return 0


def _local(iso):
    """UTC no banco, horario de quem le na tela.

    Guardar em UTC evita que o registro fique ambiguo quando o horario de
    verao volta; mostrar em UTC faria o master comparar com o relogio da
    parede e achar que esta tudo uma hora errado.
    """
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%d/%m %H:%M:%S")
    except (ValueError, TypeError):
        return iso
