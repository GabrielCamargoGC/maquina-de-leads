#!/usr/bin/env python3
r"""
Campanha de disparo: fila, ritmo e estado.

POR QUE A FILA VIVE NO DISCO
----------------------------
Uma campanha de 5 mil numeros a 4,5 segundos por envio leva mais de 6 horas.
Nesse intervalo o servico reinicia -- atualizacao de codigo, queda de luz,
Windows Update. Se a fila morasse em memoria, ao voltar ela recomecaria do
zero e mandaria de novo para quem ja recebeu.

Mandar duas vezes e o pior resultado possivel: irrita o contato e e
exatamente o padrao que faz o WhatsApp tratar o numero como spam. Por isso
cada destino e uma linha com estado proprio, e ao subir o servico so pega
quem ainda esta em 'fila'.

POR QUE UM WORKER SO
--------------------
Dois envios ao mesmo tempo quebram o ritmo -- que e a unica protecao real
contra bloqueio. A fila e serial de proposito; campanha nova espera a
anterior terminar.
"""
import random
import sqlite3
import threading
import time
import traceback
import uuid
from datetime import datetime

from . import auditoria, config, digisac

# Estados de uma campanha
RASCUNHO, RODANDO, PAUSADA, CONCLUIDA = "rascunho", "rodando", "pausada", "concluida"

# Estados de um envio. 'entregue' e 'lido' so chegam pelo webhook.
NA_FILA, ENVIADO, ENTREGUE, LIDO, ERRO, PULADO = (
    "fila", "enviado", "entregue", "lido", "erro", "pulado")

_lock = threading.Lock()
_worker = []
_acordar = threading.Event()


def _con():
    con = sqlite3.connect(config.BANCO_APP, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    return con


def criar_tabelas():
    con = _con()
    con.execute("""CREATE TABLE IF NOT EXISTS campanha (
        id TEXT PRIMARY KEY, nome TEXT NOT NULL, mensagem TEXT NOT NULL,
        criada_por TEXT, criada_em TEXT NOT NULL, iniciada_em TEXT,
        concluida_em TEXT, estado TEXT NOT NULL, total INTEGER DEFAULT 0,
        erro TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS envio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campanha_id TEXT NOT NULL, nome TEXT, numero TEXT NOT NULL,
        status TEXT NOT NULL, msg_id TEXT, erro TEXT, quando TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS optout (
        numero TEXT PRIMARY KEY, quando TEXT NOT NULL, origem TEXT, texto TEXT)""")
    # Indice em (campanha_id, status): a pergunta "qual o proximo da fila"
    # roda a cada envio, e sem ele vira varredura da tabela inteira quando a
    # campanha e grande.
    con.execute("CREATE INDEX IF NOT EXISTS ix_envio_fila "
                "ON envio (campanha_id, status)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_envio_msg ON envio (msg_id)")
    con.commit()
    con.close()


# ------------------------------------------------------------ opt-out


def esta_bloqueado(numero, con=None):
    proprio = con is None
    con = con or _con()
    try:
        return con.execute("SELECT 1 FROM optout WHERE numero=?",
                           (numero,)).fetchone() is not None
    finally:
        if proprio:
            con.close()


def bloquear(numero, origem="resposta", texto=""):
    con = _con()
    con.execute(
        "INSERT OR IGNORE INTO optout (numero, quando, origem, texto) "
        "VALUES (?,?,?,?)",
        (numero, datetime.now().isoformat(timespec="seconds"), origem, texto[:200]))
    con.commit()
    con.close()


def listar_optout(limite=200):
    con = _con()
    r = con.execute("SELECT * FROM optout ORDER BY quando DESC LIMIT ?",
                    (limite,)).fetchall()
    con.close()
    return [dict(x) for x in r]


# ------------------------------------------------------------ criar


def criar(nome, mensagem, destinos, criada_por=""):
    """destinos: lista de (nome, numero). Devolve o id da campanha.

    Numero repetido e numero em opt-out saem aqui, e nao na hora de enviar:
    a tela precisa mostrar o total verdadeiro antes de a pessoa confirmar.
    """
    criar_tabelas()
    if not mensagem.strip():
        raise ValueError("A mensagem esta vazia.")

    con = _con()
    vistos, limpos = set(), []
    for n, numero in destinos:
        numero = "".join(c for c in str(numero or "") if c.isdigit())
        if not numero or numero in vistos or esta_bloqueado(numero, con):
            continue
        vistos.add(numero)
        limpos.append((n or "", numero))

    if not limpos:
        con.close()
        raise ValueError("Nenhum destino valido: a lista esta vazia, "
                         "repetida ou toda em opt-out.")
    if len(limpos) > config.DISPARO_MAX_DESTINOS:
        con.close()
        raise ValueError(
            f"{len(limpos):,} destinos passa do teto de "
            f"{config.DISPARO_MAX_DESTINOS:,} por campanha.".replace(",", "."))

    ident = uuid.uuid4().hex[:12]
    agora = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO campanha (id, nome, mensagem, criada_por, criada_em, "
        "estado, total) VALUES (?,?,?,?,?,?,?)",
        (ident, nome or "Sem nome", mensagem, criada_por, agora, RASCUNHO,
         len(limpos)))
    con.executemany(
        "INSERT INTO envio (campanha_id, nome, numero, status) VALUES (?,?,?,?)",
        [(ident, n, num, NA_FILA) for n, num in limpos])
    con.commit()
    con.close()
    return ident


def montar_mensagem(modelo, nome):
    """Troca {nome} pelo nome da empresa.

    Sem variavel, mil pessoas recebem texto identico no mesmo dia -- que e o
    que o WhatsApp mede para decidir que aquilo e disparo em massa.
    """
    return (modelo or "").replace("{nome}", nome or "").strip()


# ------------------------------------------------------------ controle


def _mudar_estado(ident, estado, **campos):
    con = _con()
    sets = ["estado=?"]
    vals = [estado]
    for k, v in campos.items():
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(ident)
    con.execute(f"UPDATE campanha SET {', '.join(sets)} WHERE id=?", vals)
    con.commit()
    con.close()


def iniciar(ident, usuario=""):
    c = ver(ident)
    if not c:
        raise ValueError("Campanha nao encontrada.")
    if c["estado"] == RODANDO:
        return
    ok, msg = digisac.testar()
    if not ok:
        raise ValueError(f"DigiSac nao respondeu: {msg}")
    _mudar_estado(ident, RODANDO,
                  iniciada_em=datetime.now().isoformat(timespec="seconds"))
    auditoria.registrar(auditoria.DISPAROU, usuario=usuario,
                        campanha=c["nome"], destinos=c["total"])
    iniciar_worker()
    _acordar.set()


def pausar(ident, usuario=""):
    _mudar_estado(ident, PAUSADA)
    auditoria.registrar(auditoria.DISPARO_PAUSADO, usuario=usuario,
                        campanha=ident)


def ver(ident):
    con = _con()
    r = con.execute("SELECT * FROM campanha WHERE id=?", (ident,)).fetchone()
    con.close()
    return dict(r) if r else None


def contagem(ident):
    con = _con()
    r = con.execute(
        "SELECT status, count(*) n FROM envio WHERE campanha_id=? GROUP BY status",
        (ident,)).fetchall()
    con.close()
    d = {x["status"]: x["n"] for x in r}
    d["total"] = sum(d.values())
    # entregue e lido ja foram enviados; somar os tres da o que saiu.
    d["concluidos"] = (d.get(ENVIADO, 0) + d.get(ENTREGUE, 0) + d.get(LIDO, 0)
                       + d.get(ERRO, 0) + d.get(PULADO, 0))
    return d


def listar(limite=30):
    con = _con()
    r = con.execute("SELECT * FROM campanha ORDER BY criada_em DESC, rowid DESC "
                    "LIMIT ?", (limite,)).fetchall()
    con.close()
    return [dict(x) for x in r]


def envios(ident, limite=200):
    con = _con()
    r = con.execute("SELECT * FROM envio WHERE campanha_id=? "
                    "ORDER BY id LIMIT ?", (ident, limite)).fetchall()
    con.close()
    return [dict(x) for x in r]


# ------------------------------------------------------------ webhook


# Como o DigiSac nomeia o andamento, em cada uma das formas que aparecem.
_MAPA_STATUS = {
    "sent": ENVIADO, "1": ENVIADO,
    "delivered": ENTREGUE, "received": ENTREGUE, "2": ENTREGUE,
    "read": LIDO, "3": LIDO,
    "failed": ERRO, "error": ERRO, "-1": ERRO,
}


def registrar_evento(ev):
    """Aplica um evento do webhook. Nunca levanta -- quem chama e rota HTTP."""
    if not ev or not ev.get("tipo") and not ev.get("msg_id"):
        return

    # Conta com webhook "geral" recebe evento de toda conexao. So o da
    # conexao que dispara interessa.
    esperado = config.DIGISAC_SERVICE_ID
    if esperado and ev.get("service_id") and ev["service_id"] != esperado:
        return

    # Resposta do lead: pedido de parada vira opt-out na hora.
    if not ev.get("minha") and ev.get("numero") and ev.get("texto"):
        if digisac.pede_parada(ev["texto"]):
            bloquear(ev["numero"], origem="resposta", texto=ev["texto"])
            _pular_pendentes(ev["numero"])
        return

    novo = _MAPA_STATUS.get(ev.get("estado", ""))
    if not novo or not ev.get("msg_id"):
        return

    con = _con()
    # So avanca. O webhook nao garante ordem, e sem esta trava um 'sent'
    # atrasado sobrescreveria um 'read' que ja tinha chegado.
    ordem = {ENVIADO: 1, ENTREGUE: 2, LIDO: 3, ERRO: 1}
    atual = con.execute("SELECT status FROM envio WHERE msg_id=?",
                        (ev["msg_id"],)).fetchone()
    if atual and ordem.get(novo, 0) > ordem.get(atual["status"], 0):
        con.execute("UPDATE envio SET status=? WHERE msg_id=?",
                    (novo, ev["msg_id"]))
        con.commit()
    con.close()


def _pular_pendentes(numero):
    """Quem pediu para parar nao recebe o que ainda estava na fila -- em
    nenhuma campanha, nem nas que ja estao rodando."""
    con = _con()
    con.execute("UPDATE envio SET status=?, erro='opt-out' "
                "WHERE numero=? AND status=?", (PULADO, numero, NA_FILA))
    con.commit()
    con.close()


# ------------------------------------------------------------ worker


def _proximo():
    """Proximo envio pendente de alguma campanha rodando, ou None."""
    con = _con()
    r = con.execute(
        "SELECT e.*, c.mensagem, c.id AS camp FROM envio e "
        "JOIN campanha c ON c.id = e.campanha_id "
        "WHERE c.estado=? AND e.status=? ORDER BY e.id LIMIT 1",
        (RODANDO, NA_FILA)).fetchone()
    con.close()
    return dict(r) if r else None


def _marcar(envio_id, status, msg_id="", erro=""):
    con = _con()
    con.execute("UPDATE envio SET status=?, msg_id=?, erro=?, quando=? WHERE id=?",
                (status, msg_id, erro[:300],
                 datetime.now().isoformat(timespec="seconds"), envio_id))
    con.commit()
    con.close()


def _fechar_se_acabou(camp_id):
    con = _con()
    resta = con.execute("SELECT count(*) n FROM envio WHERE campanha_id=? "
                        "AND status=?", (camp_id, NA_FILA)).fetchone()["n"]
    con.close()
    if not resta:
        _mudar_estado(camp_id, CONCLUIDA,
                      concluida_em=datetime.now().isoformat(timespec="seconds"))


def _passo():
    """Um envio. Devolve True se mandou algo (e portanto deve pausar depois)."""
    item = _proximo()
    if not item:
        return False

    if esta_bloqueado(item["numero"]):
        _marcar(item["id"], PULADO, erro="opt-out")
        return False                      # nao gastou envio, nao espera

    texto = montar_mensagem(item["mensagem"], item["nome"])
    try:
        msg_id = digisac.enviar(item["numero"], texto)
        _marcar(item["id"], ENVIADO, msg_id=msg_id or "")
    except digisac.ErroDigiSac as e:
        if e.definitivo:
            _marcar(item["id"], ERRO, erro=str(e))
        else:
            # Falha de rede nao queima o destino: fica na fila para a proxima
            # volta. O que nao pode e girar rapido em cima do erro.
            _marcar(item["id"], NA_FILA, erro=str(e))
            time.sleep(30)
    except Exception as e:
        traceback.print_exc()
        _marcar(item["id"], ERRO, erro=f"Falha inesperada: {e}")

    _fechar_se_acabou(item["camp"])
    return True


def _laco():
    while True:
        try:
            mandou = _passo()
        except Exception:
            traceback.print_exc()
            mandou = False

        if mandou:
            # Intervalo sorteado dentro da faixa. Cadencia exata e assinatura
            # de robo; o sorteio e o que faz o ritmo parecer humano.
            time.sleep(random.uniform(config.DISPARO_PAUSA_MIN,
                                      config.DISPARO_PAUSA_MAX))
        else:
            # Nada na fila: dorme ate alguem iniciar campanha. O timeout
            # existe para religar sozinho se um evento se perder.
            _acordar.wait(timeout=30)
            _acordar.clear()


def iniciar_worker():
    """Sobe a thread de disparo e retoma campanha que ficou pela metade.

    Chamado na partida do site: campanha que estava rodando quando o servico
    caiu volta sozinha do ponto onde parou, sem ninguem precisar lembrar.
    """
    with _lock:
        criar_tabelas()
        if _worker:
            return
        t = threading.Thread(target=_laco, daemon=True, name="disparo")
        t.start()
        _worker.append(t)
