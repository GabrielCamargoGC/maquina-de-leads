#!/usr/bin/env python3
r"""
Job de atualizacao. Roda todos os dias as 03:00.

A Receita publica dados novos 1x por mes, nao todo dia. Rodar diariamente nao
serve para pegar "o pedaco novo" -- nao existe pedaco novo, cada publicacao e
a base inteira de novo. Serve para nunca ficar atrasado: no dia em que a
pasta nova aparece, a base ja esta convertida antes de alguem chegar.

Nos ~29 dias em que nada mudou o job custa uma requisicao HTTP e termina em
segundos, sem baixar nada.

Sequencia quando ha base nova:

    baixa 7,3 GB  ->  importa  ->  consolida  ->  VALIDA  ->  troca  ->  limpa

A validacao fica ANTES da troca de proposito: se a Receita publicar um
arquivo truncado, o job aborta e a base do mes passado continua no ar. Meia
base silenciosa e pior que base velha.

A troca para o servico do site antes de renomear as pastas porque o Windows
nao renomeia diretorio que tenha arquivo aberto -- sao alguns segundos de
indisponibilidade as 03:00.

Uso:
    python -m leads.atualizar              # confere e atualiza se preciso
    python -m leads.atualizar --forcar     # atualiza mesmo sem base nova
    python -m leads.atualizar --so-checar  # so diz se tem novidade
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config, consolidar, estado, exportar, fonte_rfb, importador

NOME_SERVICO = os.environ.get("LEADS_SERVICO", "LeadsCNPJ")
DIAS_GUARDAR_EXPORT = 7


def arquivo_log():
    return config.DIR_LOGS / f"atualizar-{datetime.now():%Y-%m}.log"


class _Tee:
    """Espelha o que sai no terminal para o arquivo de log.

    Existe porque importador e consolidar reportam progresso com print(), e
    numa execucao real a janela do PowerShell fecha (ou o usuario desconecta)
    e esse progresso se perde. Da primeira vez isso custou uma diagnose: o
    log ia so ate "3/5 consolidando" e nao dava para saber em qual balde o
    job estava quando morreu.
    """

    def __init__(self, original, caminho):
        self.original = original
        self.arquivo = open(caminho, "a", encoding="utf-8", buffering=1)

    def write(self, texto):
        self.original.write(texto)
        try:
            self.arquivo.write(texto)
        except (OSError, ValueError):
            pass  # log nunca derruba o job

    def flush(self):
        self.original.flush()
        try:
            self.arquivo.flush()
        except (OSError, ValueError):
            pass

    def fechar(self):
        try:
            self.arquivo.close()
        except (OSError, ValueError):
            pass


@contextmanager
def log_em_arquivo():
    config.DIR_LOGS.mkdir(parents=True, exist_ok=True)
    try:
        tee = _Tee(sys.stdout, arquivo_log())
    except OSError:
        yield  # sem log em arquivo e melhor que nao rodar
        return
    antigo = sys.stdout
    sys.stdout = tee
    try:
        yield
    finally:
        sys.stdout = antigo
        tee.fechar()


def _log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------ trava


class Trava:
    """Impede duas execucoes ao mesmo tempo.

    Cenario real: a atualizacao de um mes demora mais de 24 h por algum
    motivo e o agendador dispara a proxima em cima. Sem trava, as duas
    escrevem na mesma pasta.
    """

    def __init__(self, caminho):
        self.caminho = Path(caminho)
        self.fd = None

    def _dono_vivo(self):
        """O processo que criou a trava ainda existe?

        Sem isso, um job morto (janela fechada, queda de energia) deixa a
        trava para tras e bloqueia todas as execucoes seguintes ate 12 h
        depois -- inclusive as 03:00 do dia seguinte. Foi exatamente o que
        aconteceu na primeira tentativa de retomada.
        """
        try:
            pid = int(self.caminho.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return False  # trava ilegivel: trata como abandonada

        if pid == os.getpid():
            return False
        if os.name != "nt":
            try:
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, PermissionError) as e:
                return isinstance(e, PermissionError)

        # No Windows os.kill mata o processo em vez de so consultar; a
        # pergunta "existe?" se faz abrindo um handle de consulta.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            codigo = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo)):
                return codigo.value == 259  # STILL_ACTIVE
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def __enter__(self):
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, f"{os.getpid()} {datetime.now()}".encode())
            return self
        except FileExistsError:
            idade = time.time() - self.caminho.stat().st_mtime
            if not self._dono_vivo():
                _log("[aviso] trava de um processo que nao existe mais; limpando")
                self.caminho.unlink(missing_ok=True)
                return self.__enter__()
            if idade > 12 * 3600:
                _log(f"[aviso] trava com {idade/3600:.0f}h, tratando como abandonada")
                self.caminho.unlink(missing_ok=True)
                return self.__enter__()
            raise SystemExit(
                f"[erro] ja existe uma atualizacao rodando ({self.caminho}). "
                f"Se tiver certeza que nao, apague esse arquivo."
            )

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
        self.caminho.unlink(missing_ok=True)


# ------------------------------------------------------------ servico


def _estado_servico():
    """RUNNING / STOPPED / None (nao instalado). None e situacao normal em
    desenvolvimento, onde o site roda a mao."""
    if os.name != "nt":
        r = subprocess.run(["systemctl", "is-active", NOME_SERVICO],
                           capture_output=True, text=True)
        if r.returncode not in (0, 3):
            return None
        return "RUNNING" if r.stdout.strip() == "active" else "STOPPED"
    try:
        r = subprocess.run(["sc.exe", "query", NOME_SERVICO],
                           capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    saida = r.stdout.upper()
    if "RUNNING" in saida:
        return "RUNNING"
    if "STOPPED" in saida:
        return "STOPPED"
    return "TRANSICAO"


def _servico(acao, esperar=60):
    """Para/inicia o site e ESPERA a transicao terminar.

    Esperar nao e zelo: "sc stop" volta assim que o pedido e aceito, nao
    quando o processo morreu. E o Windows recusa renomear uma pasta que
    ainda tenha arquivo aberto -- que e exatamente o que a troca da base faz
    logo depois. Sem esta espera, a troca mensal falha de forma
    intermitente, dependendo de quanto o servico demora para soltar os
    arquivos.
    """
    if _estado_servico() is None:
        _log(f"  servico {NOME_SERVICO} nao instalado; seguindo sem ele")
        return False

    cmd = (["systemctl", acao, NOME_SERVICO] if os.name != "nt"
           else ["sc.exe", acao, NOME_SERVICO])
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        _log(f"  servico {NOME_SERVICO}: nao consegui {acao} ({e}); seguindo")
        return False

    alvo = "STOPPED" if acao == "stop" else "RUNNING"
    limite = time.time() + esperar
    while time.time() < limite:
        if _estado_servico() == alvo:
            _log(f"  servico {NOME_SERVICO}: {alvo.lower()}")
            return True
        time.sleep(1)

    _log(f"  [aviso] servico {NOME_SERVICO} nao chegou a {alvo} em {esperar}s "
         f"(estado: {_estado_servico()})")
    return False


# ------------------------------------------------------------ etapas


def precisa_atualizar():
    """Devolve (precisa, publicada, instalada)."""
    instalada = estado.ler().get("referencia")
    publicada = fonte_rfb.referencia_publicada()
    if publicada is None:
        raise fonte_rfb.ErroFonte("nao consegui descobrir a referencia publicada")
    return (publicada != instalada), publicada, instalada


def _renomear(origem, destino, tentativas=10):
    """os.rename com nova tentativa.

    No Windows, renomear pasta que ainda tenha arquivo aberto levanta
    PermissionError. O servico ja foi parado e esperado antes de chegar
    aqui, mas antivirus e indexador tambem seguram arquivo por alguns
    instantes -- e melhor insistir por 10s do que abortar uma atualizacao
    que levou 40 minutos.
    """
    for tentativa in range(1, tentativas + 1):
        try:
            os.rename(origem, destino)
            return
        except PermissionError:
            if tentativa == tentativas:
                raise
            time.sleep(1)


def trocar(referencia, linhas, municipios, ufs):
    """atual -> anterior -> lixo, novo -> atual. Renomear e instantaneo.

    Guardar a base anterior nao e luxo: e ela que faz a tela de "empresas
    novas" ser exata em vez de aproximada.
    """
    lixo = config.DIR_DADOS / "_lixo"
    shutil.rmtree(lixo, ignore_errors=True)

    if config.DIR_ANTERIOR.exists():
        _renomear(config.DIR_ANTERIOR, lixo)
    if config.DIR_ATUAL.exists():
        _renomear(config.DIR_ATUAL, config.DIR_ANTERIOR)
    _renomear(config.DIR_NOVO, config.DIR_ATUAL)

    estado.gravar(config.DIR_ATUAL, referencia=referencia, linhas=linhas,
                  municipios=municipios, ufs=ufs)
    shutil.rmtree(lixo, ignore_errors=True)
    _log(f"  troca feita: base {referencia} no ar")


def limpar(manter_zips=False):
    if not manter_zips:
        n = 0
        for z in Path(config.DIR_DOWNLOADS).glob("*.zip"):
            z.unlink(missing_ok=True)
            n += 1
        for z in Path(config.DIR_DOWNLOADS).glob("*.part"):
            z.unlink(missing_ok=True)
        if n:
            _log(f"  {n} zip(s) apagados de {config.DIR_DOWNLOADS}")
    try:
        removidos = exportar.limpar_antigos(DIAS_GUARDAR_EXPORT)
        if removidos:
            _log(f"  {removidos} planilha(s) antiga(s) removida(s)")
    except Exception as e:
        _log(f"  [aviso] limpeza de planilhas falhou: {e}")


def importacao_completa():
    """A conversao dos zips ja produziu tudo que a consolidacao precisa?

    Serve para o --retomar decidir se pode pular os ~9 minutos de import.
    """
    partes = ["estabelecimentos", "empresas", "simples"]
    arquivos = ["municipios.parquet", "cnaes.parquet"]
    return (all((config.DIR_NOVO / p).is_dir() and any((config.DIR_NOVO / p).iterdir())
                for p in partes)
            and all((config.DIR_NOVO / a).exists() for a in arquivos))


def executar(forcar=False, manter_zips=False, ufs_teste=None, retomar=False):
    config.garantir_pastas()
    t0 = time.time()

    precisa, publicada, instalada = precisa_atualizar()
    _log(f"Publicada na Receita: {publicada} | instalada aqui: {instalada or 'nenhuma'}")

    if not precisa and not forcar:
        _log("Nada novo. Encerrando sem baixar nada.")
        return 0

    if forcar and not precisa:
        _log("--forcar: refazendo mesmo sem base nova")

    _log("=== 1/5 baixando da Receita ===")
    referencia, qtd, bytes_ = fonte_rfb.baixar_todos(config.DIR_DOWNLOADS, forcar=False)
    _log(f"  {qtd} arquivos, {bytes_/1e9:.1f} GB")

    _log("=== 2/5 convertendo para Parquet ===")
    if retomar and importacao_completa():
        _log("  --retomar: conversao ja estava pronta, pulando")
    else:
        shutil.rmtree(config.DIR_NOVO, ignore_errors=True)
        importador.importar(config.DIR_DOWNLOADS, config.DIR_NOVO, ufs=ufs_teste)

    _log("=== 3/5 consolidando (junta e valida) ===")
    linhas = consolidar.consolidar(config.DIR_NOVO, ufs=ufs_teste, retomar=retomar)

    municipios = ufs = None
    try:
        import duckdb
        con = duckdb.connect()
        cid = (config.DIR_NOVO / "cidades.parquet").as_posix()
        municipios, ufs = con.execute(
            f"SELECT count(*), count(DISTINCT uf) FROM read_parquet('{cid}')"
        ).fetchone()
        con.close()
    except Exception as e:
        _log(f"  [aviso] nao consegui contar municipios: {e}")

    _log("=== 4/5 trocando a base no ar ===")
    _servico("stop")
    try:
        trocar(referencia or publicada, linhas, municipios, ufs)
    finally:
        _servico("start")

    _log("=== 5/5 limpando ===")
    limpar(manter_zips=manter_zips)

    _log(f"Concluido em {(time.time()-t0)/60:.0f} min. "
         f"Base {referencia or publicada}: {linhas:,} estabelecimentos.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--forcar", action="store_true",
                    help="refaz mesmo que a referencia publicada seja a mesma")
    ap.add_argument("--so-checar", action="store_true",
                    help="apenas informa se ha base nova e sai")
    ap.add_argument("--manter-zips", action="store_true",
                    help="nao apaga os zips no fim (util para depurar)")
    ap.add_argument("--ufs", help="teste: processa so estes estados, ex.: SP,PR")
    ap.add_argument("--retomar", action="store_true",
                    help="continua de onde a execucao anterior parou (nao rebaixa, "
                         "nao reconverte e pula os baldes ja completos)")
    args = ap.parse_args()

    if args.so_checar:
        precisa, publicada, instalada = precisa_atualizar()
        print(f"publicada={publicada} instalada={instalada or 'nenhuma'} "
              f"precisa_atualizar={'sim' if precisa else 'nao'}")
        return 0 if not precisa else 10

    # log_em_arquivo ANTES da Trava, de proposito. Na ordem inversa, quando a
    # trava recusa a execucao a mensagem sai antes de existir log -- e se o
    # processo foi iniciado com janela oculta, ela some sem deixar rastro.
    # Foi o que aconteceu na primeira tentativa de retomada: um lock orfao de
    # uma execucao morta barrou o job, ninguem viu o aviso, e o sintoma virou
    # "nao acontece nada".
    with log_em_arquivo(), Trava(config.DIR_DADOS / "atualizar.lock"):
        try:
            return executar(
                forcar=args.forcar, manter_zips=args.manter_zips,
                ufs_teste=args.ufs.split(",") if args.ufs else None,
                retomar=args.retomar,
            )
        except SystemExit:
            raise
        except Exception as e:
            _log(f"[ERRO] atualizacao falhou: {e}")
            import traceback
            _log(traceback.format_exc())
            _servico("start")  # garante que o site volta mesmo se falhou no meio
            return 1


if __name__ == "__main__":
    sys.exit(main())
