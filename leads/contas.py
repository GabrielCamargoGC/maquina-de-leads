#!/usr/bin/env python3
r"""
Contas, sessoes e recuperacao de senha -- sem e-mail.

Decisao central: nao existe envio de e-mail em lugar nenhum. Nada de
confirmar cadastro, nada de "link de redefinicao", nada de servidor SMTP para
manter. Em troca, a recuperacao tem duas camadas:

  1. CODIGOS DE RECUPERACAO. No cadastro saem 6 codigos de uso unico,
     mostrados uma vez so. Esqueceu a senha: usuario + um codigo -> senha
     nova, e aquele codigo morre. Mesmo mecanismo que o GitHub usa como
     reserva do autenticador.

  2. SENHA TEMPORARIA PELO MASTER. Perdeu os codigos tambem: o master gera
     uma senha valida por 24 h e entrega por fora (WhatsApp, pessoalmente).
     No primeiro acesso o sistema obriga a trocar.

Limite assumido: quem perder os codigos e nao conseguir falar com o master
fica de fora. Para uma equipe conhecida isso e aceitavel; no dia que abrir
para desconhecido, e-mail deixa de ser opcional.

Senha e codigo sao guardados com scrypt, que ja vem no Python -- nenhuma
dependencia nova entra por causa disto.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta, timezone

from . import config

# Custo do scrypt. 2**14 pede ~16 MB por verificacao: caro o bastante para
# atrapalhar quem tenta adivinhar em massa, barato o bastante para nao pesar
# num login legitimo.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1

DIAS_SESSAO = 30
HORAS_SENHA_TEMPORARIA = 24
QTD_CODIGOS = 6
MIN_SENHA = 8

# Bloqueio progressivo: a partir da 5a tentativa errada, espera dobrando.
TENTATIVAS_ANTES_DE_ESPERAR = 5
ESPERA_MAXIMA_S = 900


class ErroConta(Exception):
    """Erro que o usuario consegue corrigir -- a tela mostra a mensagem."""


# ------------------------------------------------------------ banco


_local = threading.local()


def conectar():
    """Conexao reaproveitada por thread.

    Abrir o arquivo e rodar os PRAGMA a cada chamada custava ~5 ms, e a
    sessao e conferida em TODA requisicao -- seria 5 ms somados a cada clique
    de cada pessoa, sem fazer trabalho nenhum. Guardar por thread derruba
    isso para o tempo da consulta em si.

    Por thread, e nao global: conexao SQLite compartilhada entre threads sem
    cuidado corrompe estado. O waitress atende cada requisicao numa thread do
    pool, entao cada uma tem a sua e nenhuma disputa com a outra.
    """
    caminho = str(config.BANCO_APP)
    con = getattr(_local, "con", None)
    if con is not None and getattr(_local, "caminho", None) == caminho:
        return con

    if con is not None:
        try:
            con.close()
        except sqlite3.Error:
            pass

    config.BANCO_APP.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(caminho, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    _local.con = con
    _local.caminho = caminho
    return con


def criar_tabelas():
    con = conectar()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS usuario (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario         TEXT NOT NULL UNIQUE COLLATE NOCASE,
            nome            TEXT NOT NULL DEFAULT '',
            senha           TEXT NOT NULL,
            e_master        INTEGER NOT NULL DEFAULT 0,
            ativo           INTEGER NOT NULL DEFAULT 1,
            trocar_senha    INTEGER NOT NULL DEFAULT 0,
            senha_expira_em TEXT,
            criado_em       TEXT NOT NULL,
            criado_por      TEXT NOT NULL DEFAULT '',
            ultimo_acesso   TEXT
        );

        CREATE TABLE IF NOT EXISTS codigo_recuperacao (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
            codigo     TEXT NOT NULL,
            usado_em   TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_codigo_usuario ON codigo_recuperacao(usuario_id);

        CREATE TABLE IF NOT EXISTS sessao (
            token      TEXT PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
            criado_em  TEXT NOT NULL,
            expira_em  TEXT NOT NULL,
            ip         TEXT NOT NULL DEFAULT '',
            agente     TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_sessao_usuario ON sessao(usuario_id);

        CREATE TABLE IF NOT EXISTS tentativa (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario   TEXT NOT NULL,
            ip        TEXT NOT NULL DEFAULT '',
            quando    TEXT NOT NULL,
            sucesso   INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_tentativa_usuario ON tentativa(usuario, quando);
        CREATE INDEX IF NOT EXISTS ix_tentativa_ip ON tentativa(ip, quando);
        """
    )
    con.commit()


def _agora():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt):
    return dt.isoformat()


def _de_iso(texto):
    if not texto:
        return None
    dt = datetime.fromisoformat(texto)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------ senha


def _cifrar(segredo, salt=None):
    """Devolve 'salt$hash'. Serve para senha e para codigo de recuperacao --
    codigo tambem e credencial e nao pode ficar em texto no banco."""
    salt = salt or secrets.token_bytes(16)
    bruto = hashlib.scrypt(
        segredo.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
    )
    return f"{salt.hex()}${bruto.hex()}"


def _confere(segredo, guardado):
    """Comparacao em tempo constante: com '==' o tempo de resposta vazaria
    quantos caracteres iniciais bateram."""
    try:
        salt_hex, _ = guardado.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(_cifrar(segredo, salt), guardado)


def validar_usuario(nome):
    nome = (nome or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", nome):
        raise ErroConta(
            "Usuario deve ter de 3 a 32 caracteres, usando apenas letras, "
            "numeros, ponto, hifen ou sublinhado."
        )
    return nome


def validar_senha(senha, usuario=None):
    senha = senha or ""
    if len(senha) < MIN_SENHA:
        raise ErroConta(f"A senha precisa de pelo menos {MIN_SENHA} caracteres.")
    achatada = unicodedata.normalize("NFKD", senha).casefold()
    if usuario and usuario.casefold() in achatada:
        raise ErroConta("A senha nao pode conter o nome de usuario.")
    if achatada in {"12345678", "senha123", "password", "qwertyui", "12345678910"}:
        raise ErroConta("Essa senha e facil demais de adivinhar. Escolha outra.")
    return senha


# ------------------------------------------------------------ codigos


def _formatar_codigo():
    """LEAD-XXXX-XXXX, sem caracteres que se confundem lidos no papel
    (0/O, 1/I/L). Quem anota o codigo a mao nao pode errar por causa da
    fonte."""
    alfabeto = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    parte = lambda: "".join(secrets.choice(alfabeto) for _ in range(4))
    return f"LEAD-{parte()}-{parte()}"


def gerar_codigos(usuario_id, quantidade=QTD_CODIGOS, commit=True):
    """Apaga os codigos antigos e cria novos. Devolve a lista em texto --
    e a unica vez que eles existem legiveis; no banco so fica o hash.

    commit=False permite que criar_usuario grave o usuario e os codigos na
    mesma transacao: conta criada sem codigo de recuperacao seria conta sem
    como recuperar.
    """
    con = conectar()
    con.execute("DELETE FROM codigo_recuperacao WHERE usuario_id = ?", (usuario_id,))
    codigos = [_formatar_codigo() for _ in range(quantidade)]
    con.executemany(
        "INSERT INTO codigo_recuperacao (usuario_id, codigo) VALUES (?, ?)",
        [(usuario_id, _cifrar(c)) for c in codigos],
    )
    if commit:
        con.commit()
    return codigos


def codigos_restantes(usuario_id):
    con = conectar()
    return con.execute(
        "SELECT count(*) FROM codigo_recuperacao "
        "WHERE usuario_id = ? AND usado_em IS NULL",
        (usuario_id,),
    ).fetchone()[0]


# ------------------------------------------------------------ usuarios


def existe_algum_master():
    con = conectar()
    return con.execute(
        "SELECT count(*) FROM usuario WHERE e_master = 1 AND ativo = 1"
    ).fetchone()[0] > 0


def criar_usuario(usuario, senha, nome="", e_master=False, criado_por=""):
    """Cria a conta e devolve (id, codigos_de_recuperacao).

    Os codigos so aparecem aqui. Quem chama e responsavel por mostra-los uma
    vez ao usuario -- depois disso nem o master consegue le-los de volta.
    """
    usuario = validar_usuario(usuario)
    validar_senha(senha, usuario)

    con = conectar()
    if con.execute("SELECT 1 FROM usuario WHERE usuario = ?", (usuario,)).fetchone():
        raise ErroConta(f"Ja existe um usuario chamado '{usuario}'.")
    cur = con.execute(
        "INSERT INTO usuario (usuario, nome, senha, e_master, criado_em, criado_por) "
        "VALUES (?,?,?,?,?,?)",
        (usuario, nome.strip(), _cifrar(senha), 1 if e_master else 0,
         _iso(_agora()), criado_por),
    )
    uid = cur.lastrowid
    codigos = gerar_codigos(uid, commit=False)
    con.commit()
    return uid, codigos


def buscar_usuario(usuario=None, usuario_id=None):
    con = conectar()
    if usuario_id is not None:
        r = con.execute("SELECT * FROM usuario WHERE id = ?", (usuario_id,)).fetchone()
    else:
        r = con.execute("SELECT * FROM usuario WHERE usuario = ?",
                        (str(usuario).strip(),)).fetchone()
    return dict(r) if r else None


def listar_usuarios():
    con = conectar()
    linhas = con.execute(
        """SELECT u.*,
                  (SELECT count(*) FROM codigo_recuperacao c
                   WHERE c.usuario_id = u.id AND c.usado_em IS NULL) AS codigos,
                  (SELECT count(*) FROM sessao s
                   WHERE s.usuario_id = u.id AND s.expira_em > ?) AS sessoes
           FROM usuario u ORDER BY u.e_master DESC, u.usuario""",
        (_iso(_agora()),),
    ).fetchall()
    return [dict(l) for l in linhas]


def definir_ativo(usuario_id, ativo):
    con = conectar()
    con.execute("UPDATE usuario SET ativo = ? WHERE id = ?",
                (1 if ativo else 0, usuario_id))
    if not ativo:
        # desativar precisa derrubar as sessoes abertas, senao a pessoa
        # continua usando o site ate o cookie vencer
        con.execute("DELETE FROM sessao WHERE usuario_id = ?", (usuario_id,))
    con.commit()


def trocar_senha(usuario_id, senha_nova, limpar_temporaria=True):
    u = buscar_usuario(usuario_id=usuario_id)
    if not u:
        raise ErroConta("Usuario nao encontrado.")
    validar_senha(senha_nova, u["usuario"])
    con = conectar()
    con.execute(
        "UPDATE usuario SET senha = ?, trocar_senha = 0, "
        "senha_expira_em = CASE WHEN ? THEN NULL ELSE senha_expira_em END "
        "WHERE id = ?",
        (_cifrar(senha_nova), 1 if limpar_temporaria else 0, usuario_id),
    )
    # trocar a senha invalida o que estava aberto em outros lugares
    con.execute("DELETE FROM sessao WHERE usuario_id = ?", (usuario_id,))
    con.commit()


def gerar_senha_temporaria(usuario_id):
    """Master gera, entrega por fora, e ela morre em 24 h."""
    alfabeto = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    senha = "Tmp-" + "".join(secrets.choice(alfabeto) for _ in range(4)) \
            + "-" + "".join(secrets.choice(alfabeto) for _ in range(4))
    con = conectar()
    con.execute(
        "UPDATE usuario SET senha = ?, trocar_senha = 1, senha_expira_em = ? "
        "WHERE id = ?",
        (_cifrar(senha), _iso(_agora() + timedelta(hours=HORAS_SENHA_TEMPORARIA)),
         usuario_id),
    )
    con.execute("DELETE FROM sessao WHERE usuario_id = ?", (usuario_id,))
    con.commit()
    return senha


# ------------------------------------------------------------ tentativas


def _registrar_tentativa(usuario, ip, sucesso):
    con = conectar()
    con.execute(
        "INSERT INTO tentativa (usuario, ip, quando, sucesso) VALUES (?,?,?,?)",
        (usuario or "", ip or "", _iso(_agora()), 1 if sucesso else 0),
    )
    con.execute("DELETE FROM tentativa WHERE quando < ?",
                (_iso(_agora() - timedelta(days=30)),))
    con.commit()


def espera_restante(usuario, ip):
    """Segundos que ainda faltam antes de aceitar nova tentativa.

    Conta erros seguidos do mesmo usuario OU do mesmo IP: sem a segunda
    metade, bastaria variar o nome de usuario para tentar a vontade.
    """
    con = conectar()
    desde = _iso(_agora() - timedelta(hours=1))
    erros = con.execute(
        "SELECT count(*) FROM tentativa "
        "WHERE sucesso = 0 AND quando > ? AND (usuario = ? OR (ip <> '' AND ip = ?))",
        (desde, (usuario or "").strip(), ip or ""),
    ).fetchone()[0]
    if erros < TENTATIVAS_ANTES_DE_ESPERAR:
        return 0
    ultima = con.execute(
        "SELECT quando FROM tentativa "
        "WHERE sucesso = 0 AND (usuario = ? OR (ip <> '' AND ip = ?)) "
        "ORDER BY quando DESC LIMIT 1",
        ((usuario or "").strip(), ip or ""),
    ).fetchone()
    if not ultima:
        return 0
    espera = min(2 ** (erros - TENTATIVAS_ANTES_DE_ESPERAR + 3), ESPERA_MAXIMA_S)
    passou = (_agora() - _de_iso(ultima[0])).total_seconds()
    return max(0, int(espera - passou))


# ------------------------------------------------------------ entrar


def autenticar(usuario, senha, ip="", agente=""):
    """Devolve (token_de_sessao, usuario). Levanta ErroConta com mensagem
    pronta para a tela."""
    usuario = (usuario or "").strip()
    faltam = espera_restante(usuario, ip)
    if faltam:
        raise ErroConta(
            f"Muitas tentativas erradas. Tente de novo em {faltam} segundo(s)."
        )

    u = buscar_usuario(usuario=usuario)
    # A mesma mensagem para usuario inexistente e senha errada, de proposito:
    # mensagens diferentes contam a quem tenta se aquele usuario existe.
    generico = "Usuario ou senha incorretos."

    if not u or not _confere(senha or "", u["senha"]):
        _registrar_tentativa(usuario, ip, False)
        raise ErroConta(generico)
    if not u["ativo"]:
        _registrar_tentativa(usuario, ip, False)
        raise ErroConta("Esta conta esta desativada. Fale com o administrador.")

    expira = _de_iso(u["senha_expira_em"])
    if expira and _agora() > expira:
        _registrar_tentativa(usuario, ip, False)
        raise ErroConta(
            "A senha temporaria expirou. Peca outra ao administrador."
        )

    _registrar_tentativa(usuario, ip, True)
    token = secrets.token_urlsafe(32)
    con = conectar()
    con.execute(
        "INSERT INTO sessao (token, usuario_id, criado_em, expira_em, ip, agente) "
        "VALUES (?,?,?,?,?,?)",
        (token, u["id"], _iso(_agora()),
         _iso(_agora() + timedelta(days=DIAS_SESSAO)), ip or "", (agente or "")[:200]),
    )
    con.execute("UPDATE usuario SET ultimo_acesso = ? WHERE id = ?",
                (_iso(_agora()), u["id"]))
    con.execute("DELETE FROM sessao WHERE expira_em < ?", (_iso(_agora()),))
    con.commit()
    return token, buscar_usuario(usuario_id=u["id"])


def usuario_da_sessao(token):
    if not token:
        return None
    con = conectar()
    r = con.execute(
        """SELECT u.* FROM sessao s JOIN usuario u ON u.id = s.usuario_id
           WHERE s.token = ? AND s.expira_em > ? AND u.ativo = 1""",
        (token, _iso(_agora())),
    ).fetchone()
    return dict(r) if r else None


def encerrar_sessao(token):
    if not token:
        return
    con = conectar()
    con.execute("DELETE FROM sessao WHERE token = ?", (token,))
    con.commit()


# ------------------------------------------------------------ recuperar


def recuperar_com_codigo(usuario, codigo, senha_nova, ip=""):
    """usuario + codigo de uso unico -> senha nova. O codigo e queimado."""
    usuario = (usuario or "").strip()
    faltam = espera_restante(usuario, ip)
    if faltam:
        raise ErroConta(f"Muitas tentativas erradas. Tente de novo em {faltam} segundo(s).")

    u = buscar_usuario(usuario=usuario)
    codigo = (codigo or "").strip().upper()
    generico = "Usuario ou codigo de recuperacao invalido."

    if not u or not u["ativo"]:
        _registrar_tentativa(usuario, ip, False)
        raise ErroConta(generico)

    validar_senha(senha_nova, u["usuario"])

    con = conectar()
    candidatos = con.execute(
        "SELECT id, codigo FROM codigo_recuperacao "
        "WHERE usuario_id = ? AND usado_em IS NULL",
        (u["id"],),
    ).fetchall()
    achado = next((c for c in candidatos if _confere(codigo, c["codigo"])), None)
    if not achado:
        _registrar_tentativa(usuario, ip, False)
        raise ErroConta(generico)

    con.execute("UPDATE codigo_recuperacao SET usado_em = ? WHERE id = ?",
                (_iso(_agora()), achado["id"]))
    con.execute(
        "UPDATE usuario SET senha = ?, trocar_senha = 0, senha_expira_em = NULL "
        "WHERE id = ?",
        (_cifrar(senha_nova), u["id"]),
    )
    con.execute("DELETE FROM sessao WHERE usuario_id = ?", (u["id"],))
    con.commit()

    _registrar_tentativa(usuario, ip, True)
    return codigos_restantes(u["id"])
