#!/usr/bin/env python3
r"""
Atualizar o codigo pelo proprio site, sem teclado na maquina.

Por que existe: o servidor e um desktop numa sala, sem monitor. Ligar
periferico so para rodar "git pull" nao se sustenta, e a maquina nao tem
acesso remoto instalado -- instalar exigiria justamente estar nela.

O que ISTO NAO E: nao roda comando arbitrario. Roda "git pull --ff-only" de
uma origem conferida, e mais nada. Nao ha campo onde digitar comando.

Camadas antes de alguem chegar aqui:
  1. Cloudflare Access (e-mail do dominio da empresa)
  2. sessao do site
  3. conta marcada como master
  4. token de formulario (CSRF)

E, mesmo passando por todas, o unico efeito possivel e trazer o que ja esta
publicado no repositorio -- para injetar codigo seria preciso tambem
controlar a conta do GitHub.

--ff-only e deliberado: se o historico local divergir, o comando falha em vez
de tentar juntar as pontas sozinho num servidor sem ninguem olhando.
"""
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NOME_SERVICO = os.environ.get("LEADS_SERVICO", "LeadsCNPJ")
LIMITE_S = 120


def _git(*args, timeout=LIMITE_S):
    """Roda git na pasta do projeto. Devolve (ok, saida)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(RAIZ), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        saida = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, saida.strip()
    except FileNotFoundError:
        return False, ("git nao esta instalado ou nao esta no PATH do servico. "
                       "Instale com: winget install --id Git.Git")
    except subprocess.TimeoutExpired:
        return False, f"git demorou mais de {timeout}s e foi interrompido."


def situacao():
    """Onde o codigo esta agora, sem mexer em nada."""
    ok_ramo, ramo = _git("rev-parse", "--abbrev-ref", "HEAD")
    ok_org, origem = _git("remote", "get-url", "origin")
    _, atual = _git("log", "-1", "--format=%h %s")
    # --untracked-files=no de proposito: arquivo NOVO na pasta nao corre
    # risco num pull, porque o git so sobrescreve o que ele rastreia. Sem
    # esse cuidado, as pastas que o proprio programa cria (exports/, tools/,
    # .tmp/) bloqueavam a atualizacao como se fossem alteracao de codigo --
    # foi o que aconteceu na primeira vez que o botao foi usado.
    ok_limpo, sujo = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "ok": ok_ramo and ok_org,
        "ramo": ramo if ok_ramo else "?",
        "origem": origem if ok_org else "?",
        "commit_atual": atual,
        "tem_mudanca_local": bool(sujo.strip()) if ok_limpo else False,
        "mudanca_local": sujo.strip(),
    }


def conferir():
    """Busca o que ha de novo no repositorio, sem aplicar.

    Passo separado de proposito: quem aperta o botao ve a lista do que vai
    entrar antes de decidir. Atualizar as cegas num servidor sem tela e
    exatamente o que nao queremos.
    """
    est = situacao()
    if not est["ok"]:
        return est | {"erro": "Nao consegui ler o repositorio nesta pasta.",
                      "pendentes": []}

    ok, saida = _git("fetch", "--quiet", "origin")
    if not ok:
        return est | {"erro": f"Falha ao consultar o GitHub: {saida}",
                      "pendentes": []}

    ok, lista = _git("log", "--oneline", f"HEAD..origin/{est['ramo']}")
    if not ok:
        return est | {"erro": f"Nao consegui comparar com o GitHub: {lista}",
                      "pendentes": []}

    pendentes = [l for l in lista.splitlines() if l.strip()]
    return est | {"erro": None, "pendentes": pendentes}


def aplicar():
    """git pull --ff-only. Devolve (ok, mensagem)."""
    est = situacao()
    if not est["ok"]:
        return False, "Nao consegui ler o repositorio nesta pasta."

    if est["tem_mudanca_local"]:
        return False, (
            "Ha arquivos alterados na maquina que nao estao no GitHub. "
            "Atualizar por cima apagaria essas mudancas, entao parei aqui.\n\n"
            + est["mudanca_local"]
        )

    ok, saida = _git("pull", "--ff-only", "origin", est["ramo"])
    if not ok:
        return False, (
            "O pull nao passou. Isso costuma acontecer quando o historico "
            "local seguiu por outro caminho e nao da para avancar em linha "
            "reta.\n\n" + saida
        )
    return True, saida or "Ja estava atualizado."


def instalar_dependencias():
    """Roda o pip do ambiente do projeto, caso o requirements tenha mudado."""
    python = RAIZ / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python")
    if not python.exists():
        python = Path(sys.executable)
    try:
        r = subprocess.run(
            [str(python), "-m", "pip", "install", "-r",
             str(RAIZ / "requirements.txt"), "--quiet"],
            capture_output=True, text=True, timeout=300,
        )
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def reiniciar_servico():
    """Reinicia o site DEPOIS que esta resposta ja tiver saido.

    O site e o proprio servico. Mandar reiniciar aqui dentro mataria o
    processo no meio da requisicao e o navegador mostraria erro de conexao
    justamente quando deu certo. Por isso a ordem sai para um processo
    solto, que espera alguns segundos antes de agir.
    """
    if os.name == "nt":
        comando = (
            f'ping -n 4 127.0.0.1 > nul & '
            f'net stop "{NOME_SERVICO}" & '
            f'net start "{NOME_SERVICO}"'
        )
        criar = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            subprocess.Popen(["cmd", "/c", comando], creationflags=criar,
                             close_fds=True)
            return True, "Reinicio agendado. O site volta em alguns segundos."
        except OSError as e:
            return False, f"Nao consegui agendar o reinicio: {e}"

    try:
        subprocess.Popen(
            ["sh", "-c", f"sleep 3; systemctl restart {NOME_SERVICO}"],
            start_new_session=True, close_fds=True,
        )
        return True, "Reinicio agendado. O site volta em alguns segundos."
    except OSError as e:
        return False, f"Nao consegui agendar o reinicio: {e}"
