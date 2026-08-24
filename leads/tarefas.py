#!/usr/bin/env python3
r"""
Tarefa longa disparada pelo site, com progresso na tela.

Existe por causa do indice de consulta: refaze-lo leva dezenas de minutos e
so podia ser feito com teclado na maquina. Como e justamente ele que faz a
consulta por nome e telefone ser rapida, e como ele precisa ser refeito
sempre que sua estrutura muda, exigir presenca fisica para isso nao se
sustenta.

Nao e fila de propositos gerais: roda UMA tarefa por vez, no processo do
site, e o estado vive em memoria. Se o servico reiniciar no meio, o estado
se perde -- e tudo bem, porque a tarefa monta em pasta provisoria e o que
sobra e descartavel.

O progresso e por ETAPA, nao por porcentagem de linhas. O DuckDB nao reporta
andamento dentro de um COPY, e inventar uma barra que anda sozinha seria
mentir. Tres etapas concluidas de tres e informacao verdadeira.
"""
import threading
import time
import traceback

_lock = threading.Lock()
_estado = {
    "nome": None,
    "rodando": False,
    "etapa": 0,
    "total_etapas": 0,
    "rotulo": "",
    "iniciado_em": None,
    "terminado_em": None,
    "erro": None,
    "resultado": None,
}


def estado():
    with _lock:
        d = dict(_estado)
    if d["iniciado_em"]:
        fim = d["terminado_em"] or time.time()
        d["segundos"] = int(fim - d["iniciado_em"])
    else:
        d["segundos"] = 0
    total = d["total_etapas"] or 1
    # A barra so avanca quando uma etapa TERMINA. Enquanto a primeira roda
    # ela fica em zero, o que e honesto: nao ha como saber quanto falta
    # dentro de um COPY do DuckDB.
    d["pct"] = min(100, int(d["etapa"] / total * 100))
    return d


def rodando():
    with _lock:
        return _estado["rodando"]


def _progresso(etapa, rotulo, total):
    with _lock:
        _estado["etapa"] = etapa
        _estado["rotulo"] = rotulo
        _estado["total_etapas"] = total


def iniciar(nome, funcao, total_etapas=1):
    """Dispara funcao(progresso) numa thread. Devolve (ok, mensagem).

    Recusa se ja houver tarefa em andamento -- duas geracoes de indice ao
    mesmo tempo brigariam pela mesma pasta provisoria e pela memoria da
    maquina.
    """
    with _lock:
        if _estado["rodando"]:
            return False, f"Ja existe uma tarefa em andamento: {_estado['nome']}."
        _estado.update(nome=nome, rodando=True, etapa=0,
                       total_etapas=total_etapas, rotulo="preparando",
                       iniciado_em=time.time(), terminado_em=None,
                       erro=None, resultado=None)

    def alvo():
        try:
            resultado = funcao(_progresso)
            with _lock:
                _estado["resultado"] = resultado
        except Exception as e:
            traceback.print_exc()
            with _lock:
                _estado["erro"] = str(e)
        finally:
            with _lock:
                _estado["rodando"] = False
                _estado["terminado_em"] = time.time()

    threading.Thread(target=alvo, daemon=True).start()
    return True, "Tarefa iniciada."
