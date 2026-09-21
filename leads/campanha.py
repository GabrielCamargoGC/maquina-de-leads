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
from datetime import date, datetime, timedelta

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
    # Resposta do lead. Colunas adicionadas depois, entao ALTER TABLE com
    # try: o banco do servidor ja existe com envios gravados.
    for coluna, tipo in (("respondeu_em", "TEXT"), ("resposta", "TEXT")):
        try:
            con.execute(f"ALTER TABLE envio ADD COLUMN {coluna} {tipo}")
        except sqlite3.OperationalError:
            pass
    con.execute("CREATE INDEX IF NOT EXISTS ix_envio_fila "
                "ON envio (campanha_id, status)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_envio_numero ON envio (numero)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_envio_msg ON envio (msg_id)")
    # Ultimos eventos crus do webhook, para conferencia.
    #
    # O formato que o DigiSac manda nao esta publicado em lugar que de para
    # ler sem login, entao ler_evento() procura cada campo em mais de um
    # nome. Guardar o corpo como chegou e o que permite corrigir o
    # mapeamento com o dado real na mao em vez de continuar adivinhando --
    # e e a unica forma de a tela mostrar "chegou, mas nao entendi".
    con.execute("""CREATE TABLE IF NOT EXISTS ajuste (
        chave TEXT PRIMARY KEY, valor TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS webhook_bruto (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quando TEXT NOT NULL, corpo TEXT, entendido INTEGER DEFAULT 0,
        resumo TEXT)""")
    con.commit()
    con.close()


# Quantos eventos crus ficam guardados. E instrumento de conferencia, nao
# historico: 50 cobrem um teste inteiro e nao deixam a tabela crescer sozinha.
MAX_BRUTOS = 50


def registrar_bruto(corpo, ev=None):
    """Guarda o evento como chegou, e o que conseguimos entender dele."""
    import json as _json
    try:
        texto = _json.dumps(corpo, ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        texto = str(corpo)[:4000]

    entendido = bool(ev and (ev.get("estado") or ev.get("texto")))
    resumo = ""
    if ev:
        partes = [ev.get("tipo") or "sem tipo"]
        if ev.get("estado"):
            partes.append("status=" + ev["estado"])
        if ev.get("numero"):
            partes.append(ev["numero"])
        if ev.get("texto"):
            partes.append('"' + ev["texto"][:40] + '"')
        resumo = " · ".join(partes)

    con = _con()
    con.execute("INSERT INTO webhook_bruto (quando, corpo, entendido, resumo) "
                "VALUES (?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), texto,
                 1 if entendido else 0, resumo))
    con.execute("DELETE FROM webhook_bruto WHERE id NOT IN "
                "(SELECT id FROM webhook_bruto ORDER BY id DESC LIMIT ?)",
                (MAX_BRUTOS,))
    con.commit()
    con.close()


def listar_brutos(limite=20):
    con = _con()
    r = con.execute("SELECT * FROM webhook_bruto ORDER BY id DESC LIMIT ?",
                    (limite,)).fetchall()
    con.close()
    return [dict(x) for x in r]


# ------------------------------------------------------------ ajustes


# Curva de aquecimento: dia util desde o inicio -> teto de envios no dia.
#
# Numero novo em rajada e o gatilho mais obvio de bloqueio. Subir devagar nao
# impede denuncia -- denuncia e o que derruba de verdade -- mas da tempo de
# medir resposta e erro com 50 antes de arriscar 1.200.
CURVA_AQUECIMENTO = ((3, 50), (7, 100), (14, 250))
TETO_AQUECIDO = 500

PADROES = {
    "aquecimento": "1",
    "aquecimento_inicio": "",      # vazio = comeca hoje na primeira consulta
    "janela_inicio": "8",
    "janela_fim": "18",
    "so_dias_uteis": "1",
    "freio_erros": "10",
}


def ajuste(chave, padrao=None):
    con = _con()
    try:
        r = con.execute("SELECT valor FROM ajuste WHERE chave=?", (chave,)).fetchone()
    except sqlite3.OperationalError:
        return PADROES.get(chave, padrao)
    finally:
        con.close()
    if r is None or r["valor"] is None:
        return PADROES.get(chave, padrao)
    return r["valor"]


def ajuste_int(chave):
    try:
        return int(str(ajuste(chave)).strip())
    except (TypeError, ValueError):
        return int(PADROES.get(chave, 0) or 0)


def ajuste_liga(chave):
    return str(ajuste(chave)).strip() in ("1", "on", "true", "sim")


def gravar_ajustes(valores):
    criar_tabelas()
    con = _con()
    try:
        for k, v in (valores or {}).items():
            con.execute("INSERT INTO ajuste (chave, valor) VALUES (?,?) "
                        "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
                        (k, "" if v is None else str(v)))
        con.commit()
    finally:
        con.close()


def _dias_uteis_desde(inicio):
    """Dias uteis de inicio ate hoje, contando hoje. Fim de semana nao conta
    porque nao se dispara nele -- contar daria salto de teto sem envio."""
    hoje = date.today()
    if inicio > hoje:
        return 1
    n, d = 0, inicio
    while d <= hoje:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return max(n, 1)


def inicio_aquecimento():
    """Data em que o numero comecou a aquecer. Grava na primeira consulta, e
    nao na instalacao: contar desde antes do primeiro disparo daria teto alto
    para um numero que nunca mandou nada."""
    bruto = (ajuste("aquecimento_inicio") or "").strip()
    if bruto:
        try:
            return date.fromisoformat(bruto)
        except ValueError:
            pass
    hoje = date.today()
    gravar_ajustes({"aquecimento_inicio": hoje.isoformat()})
    return hoje


def teto_do_dia():
    """(teto, dia_util). Teto 0 significa sem limite."""
    if not ajuste_liga("aquecimento"):
        return 0, 0
    dia = _dias_uteis_desde(inicio_aquecimento())
    for limite, teto in CURVA_AQUECIMENTO:
        if dia <= limite:
            return teto, dia
    return TETO_AQUECIDO, dia


def enviados_hoje():
    """Quantos sairam hoje, somando todas as campanhas.

    O teto e do numero, nao da campanha: duas campanhas no mesmo dia dividem
    o mesmo teto, senao o limite nao limitaria nada.
    """
    con = _con()
    try:
        return con.execute(
            "SELECT count(*) AS n FROM envio WHERE quando >= ? AND status <> ?",
            (date.today().isoformat(), NA_FILA)).fetchone()["n"]
    finally:
        con.close()


def dentro_da_janela(agora=None):
    """(pode_enviar, motivo). Mensagem comercial fora de hora gera denuncia,
    e denuncia e o que derruba o numero."""
    agora = agora or datetime.now()
    if ajuste_liga("so_dias_uteis") and agora.weekday() >= 5:
        return False, "fim de semana"
    ini, fim = ajuste_int("janela_inicio"), ajuste_int("janela_fim")
    if ini == fim:
        return True, ""
    if not (ini <= agora.hour < fim):
        return False, f"fora do horario ({ini}h as {fim}h)"
    return True, ""


def pode_enviar_agora():
    """(pode, motivo) juntando janela e teto do dia."""
    ok, motivo = dentro_da_janela()
    if not ok:
        return False, motivo
    teto, _ = teto_do_dia()
    if teto and enviados_hoje() >= teto:
        return False, f"teto de {teto} do dia atingido"
    return True, ""


def situacao_aquecimento():
    """O que a tela mostra sobre o aquecimento do numero."""
    teto, dia = teto_do_dia()
    hoje = enviados_hoje()
    pode, motivo = pode_enviar_agora()
    return {
        "ligado": ajuste_liga("aquecimento"),
        "inicio": inicio_aquecimento().isoformat(),
        "dia": dia, "teto": teto, "hoje": hoje,
        "restam_hoje": max(teto - hoje, 0) if teto else None,
        "pode": pode, "motivo": motivo,
        "janela": f"{ajuste_int('janela_inicio')}h as {ajuste_int('janela_fim')}h",
        "so_dias_uteis": ajuste_liga("so_dias_uteis"),
        "freio_erros": ajuste_int("freio_erros"),
    }


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


def levantar_destinos(filtros, fonte="busca", dir_dados=None, teto=None):
    """Destinos de disparo a partir dos filtros de uma busca.

    Devolve (lista, resumo). A lista ja vem deduplicada por numero e so com
    celular -- fixo raramente tem WhatsApp e so derruba a entrega. O resumo
    e o que a tela mostra ANTES de confirmar: quantos a busca achou, quantos
    tem celular, quantos estao em opt-out.

    Para de ler ao passar do teto. Sem isso, uma busca de cidade grande
    montaria 200 mil linhas em memoria so para a tela recusar depois.
    """
    from . import exportar

    teto = config.DISPARO_MAX_DESTINOS if teto is None else teto
    criar_tabelas()
    con = _con()
    try:
        lista, bloqueados, passou = [], 0, False
        for nome, numero in exportar._linhas_disparo(filtros, fonte, dir_dados):
            if esta_bloqueado(numero, con):
                bloqueados += 1
                continue
            if len(lista) >= teto:
                passou = True
                break
            lista.append((nome, numero))
    finally:
        con.close()

    return lista, {
        "com_celular": len(lista) + bloqueados,
        "em_optout": bloqueados,
        "vao_receber": len(lista),
        "passou_do_teto": passou,
        "teto": teto,
    }


def tempo_estimado(quantos):
    """Segundos que a campanha deve levar, pelo ritmo medio configurado.

    Vai na tela porque a ordem de grandeza muda a decisao: 40 numeros e
    cafe, 5 mil atravessa a tarde e chega em horario que irrita.
    """
    medio = (config.DISPARO_PAUSA_MIN + config.DISPARO_PAUSA_MAX) / 2
    return int(quantos * medio)


def montar_mensagem(modelo, nome):
    """Troca {nome} e {primeiro} no texto da mensagem.

    {nome}     -> "Eluiza Helena dos Reis Crepaldi"
    {primeiro} -> "Eluiza"

    Dois e nao um porque o certo depende do lead: MEI e pessoa fisica e
    primeiro nome soa humano; empresa com nome fantasia quer o nome todo.
    Quem escreve a mensagem sabe qual dos dois esta buscando.

    Sem variavel nenhuma, mil pessoas recebem texto identico no mesmo dia --
    que e o que o WhatsApp mede para decidir que aquilo e disparo em massa.
    """
    texto = (modelo or "")
    nome = nome or ""
    return (texto.replace("{primeiro}", nome.split()[0] if nome.split() else "")
                 .replace("{nome}", nome).strip())


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


def relatorio(ident):
    """Numeros da campanha, do jeito que se le num relatorio.

    contagem() devolve o estado cru de cada envio; aqui os estados viram as
    perguntas que quem disparou faz de verdade: chegou? leu? deu erro em
    quantos e por que?

    'entregue' e 'lido' sao exclusivos no banco -- quem leu esta em 'lido' e
    nao conta duas vezes. Entao "chegou" e a soma dos dois, e "nao lido" e so
    o que ficou em 'entregue'.
    """
    c = ver(ident) or {}
    d = contagem(ident)

    entregue = d.get(ENTREGUE, 0)
    lido = d.get(LIDO, 0)
    enviado = d.get(ENVIADO, 0)
    erro = d.get(ERRO, 0)
    pulado = d.get(PULADO, 0)
    fila = d.get(NA_FILA, 0)
    saiu = enviado + entregue + lido

    con = _con()
    try:
        responderam = con.execute(
            "SELECT count(*) AS n FROM envio WHERE campanha_id=? "
            "AND respondeu_em IS NOT NULL", (ident,)).fetchone()["n"]
        motivos = con.execute(
            "SELECT coalesce(erro, '(sem detalhe)') AS motivo, count(*) AS n "
            "FROM envio WHERE campanha_id=? AND status=? "
            "GROUP BY motivo ORDER BY n DESC LIMIT 8",
            (ident, ERRO)).fetchall()
    finally:
        con.close()

    def pct(n):
        return round(100 * n / saiu) if saiu else 0

    return {
        "estado": c.get("estado", ""),
        "total": d.get("total", 0),
        "saiu": saiu,
        "chegou": entregue + lido,
        "lido": lido,
        "nao_lido": entregue,
        # Sem confirmacao nao e o mesmo que nao chegou: pode ter chegado e o
        # webhook nao ter contado. A tela precisa dizer isso com essas
        # palavras, senao vira "minhas mensagens nao foram entregues".
        "sem_confirmacao": enviado,
        "erro": erro,
        # Resposta e o unico numero aqui que mede resultado, e nao entrega.
        # Vai sobre quem recebeu, nao sobre quem foi enviado: cobrar resposta
        # de mensagem que nao chegou nao mede nada.
        "responderam": responderam,
        "pct_responderam": (round(100 * responderam / (entregue + lido))
                            if (entregue + lido) else 0),
        "pulado": pulado,
        "fila": fila,
        "pct_chegou": pct(entregue + lido),
        "pct_lido": pct(lido),
        "pct_sem": pct(enviado),
        "motivos": [dict(m) for m in motivos],
    }


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

    # Resposta do lead. Dois destinos: o contador de respostas da campanha e,
    # se for pedido de parada, o opt-out.
    if not ev.get("minha") and ev.get("numero") and ev.get("texto"):
        _anotar_resposta(ev["numero"], ev["texto"])
        if digisac.pede_parada(ev["texto"]):
            bloquear(ev["numero"], origem="resposta", texto=ev["texto"])
            _pular_pendentes(ev["numero"])
        return

    # Evento de conexao: o DigiSac avisa que o chip caiu, e isso chega antes
    # de o proximo envio falhar. Pausar aqui poupa os destinos que a fila
    # tentaria nesse meio tempo.
    if "service" in (ev.get("tipo") or "").lower() and ev.get("caiu"):
        pausar_rodando_por_queda(ev.get("estado", "")[:200])
        return

    novo = _MAPA_STATUS.get(ev.get("estado", ""))
    if not novo:
        return

    con = _con()
    try:
        # Acha o envio por id da mensagem; se nao der, pelo numero.
        #
        # O fallback nao e luxo: se a resposta do POST /messages nao trouxer
        # o id no campo que esperamos, msg_id fica vazio em TODO envio e
        # nenhum evento acha nada -- a campanha inteira fica parada em
        # "enviado" com as mensagens entregues no celular das pessoas.
        # Pelo numero sempre da, porque numero e o que nos mesmos mandamos.
        linha = None
        if ev.get("msg_id"):
            linha = con.execute(
                "SELECT id, status FROM envio WHERE msg_id=?",
                (ev["msg_id"],)).fetchone()
        if linha is None and ev.get("numero"):
            linha = con.execute(
                "SELECT id, status FROM envio WHERE numero=? AND status<>? "
                "ORDER BY id DESC LIMIT 1", (ev["numero"], NA_FILA)).fetchone()
        if linha is None:
            return

        # So avanca. O webhook nao garante ordem, e sem esta trava um 'sent'
        # atrasado sobrescreveria um 'read' que ja tinha chegado.
        ordem = {ENVIADO: 1, ENTREGUE: 2, LIDO: 3, ERRO: 1}
        if ordem.get(novo, 0) > ordem.get(linha["status"], 0):
            # Grava o msg_id quando ele chega pelo evento e faltava no envio:
            # do segundo evento em diante o casamento volta a ser exato.
            if ev.get("msg_id"):
                con.execute("UPDATE envio SET status=?, msg_id=? WHERE id=?",
                            (novo, ev["msg_id"], linha["id"]))
            else:
                con.execute("UPDATE envio SET status=? WHERE id=?",
                            (novo, linha["id"]))
            con.commit()
    finally:
        con.close()


def _anotar_resposta(numero, texto):
    """Marca que este numero respondeu, no envio mais recente que saiu.

    Guarda a PRIMEIRA resposta e nao sobrescreve: quem manda tres mensagens
    seguidas respondeu uma vez, e o contador da campanha nao pode contar tres.
    A primeira tambem e a que interessa ler -- e a reacao a mensagem.

    Numero que nunca recebeu campanha nao acha envio e e ignorado: e alguem
    falando com a empresa por conta propria, que nao e resultado de disparo.
    """
    con = _con()
    try:
        linha = con.execute(
            "SELECT id FROM envio WHERE numero=? AND status<>? "
            "AND respondeu_em IS NULL ORDER BY id DESC LIMIT 1",
            (numero, NA_FILA)).fetchone()
        if linha is None:
            return
        con.execute("UPDATE envio SET respondeu_em=?, resposta=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"),
                     (texto or "")[:400], linha["id"]))
        con.commit()
    finally:
        con.close()


def respostas(ident, limite=100):
    """Quem respondeu naquela campanha, e o que disse."""
    con = _con()
    try:
        r = con.execute(
            "SELECT nome, numero, resposta, respondeu_em FROM envio "
            "WHERE campanha_id=? AND respondeu_em IS NOT NULL "
            "ORDER BY respondeu_em DESC LIMIT ?", (ident, limite)).fetchall()
    finally:
        con.close()
    return [dict(x) for x in r]


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


_erros_seguidos = {"n": 0}


def _contar_erro(camp_id, usuario=""):
    """Pausa a campanha depois de N erros em sequencia.

    Sequencia de falhas e o sinal mais precoce de que o numero esta sendo
    marcado -- ou de que a lista esta ruim. Sem o freio, a fila continua
    batendo ate o fim e transforma um problema de 10 envios num de mil.

    O contador zera a cada envio que da certo: erro espalhado e normal
    (numero que nao existe), erro em sequencia nao e.
    """
    limite = ajuste_int("freio_erros")
    _erros_seguidos["n"] += 1
    if limite <= 0 or _erros_seguidos["n"] < limite:
        return False
    _erros_seguidos["n"] = 0
    _mudar_estado(camp_id, PAUSADA,
                  erro=f"Pausada sozinha: {limite} erros em sequencia. "
                       f"Confira a conexao no DigiSac e os numeros da lista.")
    auditoria.registrar(auditoria.DISPARO_PAUSADO, usuario="automatico",
                        campanha=camp_id, motivo=f"{limite} erros seguidos")
    return True


def pausar_por_queda(camp_id, detalhe=""):
    """Pausa por conexao caida, com recado que diz o que fazer.

    Separado do freio por erro porque a acao e outra: erro de numero se
    resolve limpando a lista, queda de conexao se resolve lendo o QR no
    painel do DigiSac. Mensagem generica aqui manda a pessoa procurar no
    lugar errado.
    """
    _erros_seguidos["n"] = 0
    _mudar_estado(camp_id, PAUSADA,
                  erro="Pausada sozinha: a conexao do WhatsApp caiu no "
                       "DigiSac. Reconecte o numero la (vai pedir o QR) e "
                       "clique em Retomar. Os destinos que falharam por isso "
                       "voltaram para a fila -- nenhum lead foi perdido.")
    auditoria.registrar(auditoria.DISPARO_PAUSADO, usuario="automatico",
                        campanha=camp_id, motivo="conexao caiu",
                        detalhe=detalhe[:200])


def pausar_rodando_por_queda(detalhe=""):
    """Pausa toda campanha rodando. Chamado quando o webhook avisa que a
    conexao caiu -- que costuma chegar antes do proximo envio falhar."""
    con = _con()
    try:
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM campanha WHERE estado=?", (RODANDO,)).fetchall()]
    finally:
        con.close()
    for i in ids:
        pausar_por_queda(i, detalhe)
    return len(ids)


def contar_erros_de_conexao(ident):
    """Quantos erros daquela campanha foram culpa da conexao, e nao do numero.

    A tela precisa do numero para nao oferecer "devolver para a fila" quando
    o que falhou foi o dado -- devolver numero que nao existe so o faria
    falhar de novo.
    """
    con = _con()
    try:
        linhas = con.execute(
            "SELECT erro FROM envio WHERE campanha_id=? AND status=?",
            (ident, ERRO)).fetchall()
    finally:
        con.close()
    return sum(1 for r in linhas if digisac.e_queda_de_conexao(r["erro"]))


def devolver_erros(ident):
    """Devolve para a fila os destinos que falharam por causa da conexao.

    Erro de conexao nao e culpa do numero: o lead nunca foi tentado de
    verdade. Sem isto, uma queda de 30 segundos custa permanentemente todos
    os destinos que passaram pela fila naquele intervalo.

    Numero que nao existe no WhatsApp continua marcado: esse erro e do dado
    e repetir daria o mesmo.
    """
    con = _con()
    try:
        alvos = [r["id"] for r in con.execute(
            "SELECT id, erro FROM envio WHERE campanha_id=? AND status=?",
            (ident, ERRO)).fetchall() if digisac.e_queda_de_conexao(r["erro"])]
        if alvos:
            marcas = ",".join("?" for _ in alvos)
            con.execute(f"UPDATE envio SET status=?, erro=NULL, quando=NULL "
                        f"WHERE id IN ({marcas})", [NA_FILA, *alvos])
            con.commit()
        return len(alvos)
    finally:
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

    # Janela de horario e teto do dia sao checados AQUI, e nao ao iniciar a
    # campanha: ela roda por horas e atravessa o fim do horario comercial. A
    # campanha fica 'rodando' e simplesmente nao consome a fila -- retoma
    # sozinha quando a janela abre ou o dia vira.
    pode, _motivo = pode_enviar_agora()
    if not pode:
        return False

    if esta_bloqueado(item["numero"]):
        _marcar(item["id"], PULADO, erro="opt-out")
        return False                      # nao gastou envio, nao espera

    texto = montar_mensagem(item["mensagem"], item["nome"])
    try:
        msg_id = digisac.enviar(item["numero"], texto)
        _marcar(item["id"], ENVIADO, msg_id=msg_id or "")
        _erros_seguidos["n"] = 0          # deu certo: zera o freio
    except digisac.ErroDigiSac as e:
        if digisac.e_queda_de_conexao(str(e)):
            # Chip caiu do WhatsApp. Pausa JA, sem esperar o freio de 10:
            # o primeiro erro desses ja diz tudo, e insistir so transforma
            # uma queda de conexao numa lista de destinos queimados.
            _marcar(item["id"], NA_FILA, erro=str(e))
            pausar_por_queda(item["camp"], str(e))
            return False
        if e.definitivo:
            _marcar(item["id"], ERRO, erro=str(e))
            if _contar_erro(item["camp"]):
                return False              # pausou: nao espera o intervalo
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
            # Nada para fazer agora. Dorme ate alguem iniciar campanha, ou
            # ate a janela reabrir.
            #
            # Espera em fatias de 5 minutos e nao ate o horario exato: teto e
            # janela mudam pelo painel, e uma espera longa deixaria a mudanca
            # sem efeito ate o dia seguinte.
            _acordar.wait(timeout=300 if _tem_fila_esperando() else 30)
            _acordar.clear()


def _tem_fila_esperando():
    """Existe campanha rodando com fila, mas travada por janela ou teto?"""
    try:
        return bool(_proximo()) and not pode_enviar_agora()[0]
    except Exception:
        return False


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
