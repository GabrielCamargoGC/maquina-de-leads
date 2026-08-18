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
from datetime import datetime
from pathlib import Path

from . import config, consolidar, estado, exportar, fonte_rfb, importador

NOME_SERVICO = os.environ.get("LEADS_SERVICO", "LeadsCNPJ")
DIAS_GUARDAR_EXPORT = 7


def _log(msg):
    linha = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(linha, flush=True)
    try:
        config.DIR_LOGS.mkdir(parents=True, exist_ok=True)
        arq = config.DIR_LOGS / f"atualizar-{datetime.now():%Y-%m}.log"
        with open(arq, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass  # nao deixar o log derrubar o job


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

    def __enter__(self):
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, f"{os.getpid()} {datetime.now()}".encode())
            return self
        except FileExistsError:
            idade = time.time() - self.caminho.stat().st_mtime
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


def _servico(acao):
    """Para/inicia o site. Silencioso quando o servico nao existe -- em
    desenvolvimento o site roda a mao e nao ha nada para parar."""
    if os.name != "nt":
        cmd = ["systemctl", acao, NOME_SERVICO]
    else:
        cmd = ["sc", acao, NOME_SERVICO]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            _log(f"  servico {NOME_SERVICO}: {acao} ok")
            return True
        _log(f"  servico {NOME_SERVICO}: {acao} devolveu {r.returncode} "
             f"(provavelmente nao instalado; seguindo)")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        _log(f"  servico {NOME_SERVICO}: nao consegui {acao} ({e}); seguindo")
    return False


# ------------------------------------------------------------ etapas


def precisa_atualizar():
    """Devolve (precisa, publicada, instalada)."""
    instalada = estado.ler().get("referencia")
    publicada = fonte_rfb.referencia_publicada()
    if publicada is None:
        raise fonte_rfb.ErroFonte("nao consegui descobrir a referencia publicada")
    return (publicada != instalada), publicada, instalada


def trocar(referencia, linhas, municipios, ufs):
    """atual -> anterior -> lixo, novo -> atual. Renomear e instantaneo.

    Guardar a base anterior nao e luxo: e ela que faz a tela de "empresas
    novas" ser exata em vez de aproximada.
    """
    lixo = config.DIR_DADOS / "_lixo"
    shutil.rmtree(lixo, ignore_errors=True)

    if config.DIR_ANTERIOR.exists():
        os.rename(config.DIR_ANTERIOR, lixo)
    if config.DIR_ATUAL.exists():
        os.rename(config.DIR_ATUAL, config.DIR_ANTERIOR)
    os.rename(config.DIR_NOVO, config.DIR_ATUAL)

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


def executar(forcar=False, manter_zips=False, ufs_teste=None):
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
    shutil.rmtree(config.DIR_NOVO, ignore_errors=True)
    importador.importar(config.DIR_DOWNLOADS, config.DIR_NOVO, ufs=ufs_teste)

    _log("=== 3/5 consolidando (junta e valida) ===")
    linhas = consolidar.consolidar(config.DIR_NOVO, ufs=ufs_teste)

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
    args = ap.parse_args()

    if args.so_checar:
        precisa, publicada, instalada = precisa_atualizar()
        print(f"publicada={publicada} instalada={instalada or 'nenhuma'} "
              f"precisa_atualizar={'sim' if precisa else 'nao'}")
        return 0 if not precisa else 10

    with Trava(config.DIR_DADOS / "atualizar.lock"):
        try:
            return executar(
                forcar=args.forcar, manter_zips=args.manter_zips,
                ufs_teste=args.ufs.split(",") if args.ufs else None,
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
