#!/usr/bin/env python3
r"""
Telas de acesso: home, entrar, cadastrar, recuperar, trocar senha, sair.

Como nasce o primeiro master: o cadastro so fica aberto enquanto NAO existe
nenhum master. A primeira pessoa que se cadastrar vira o master, e a partir
dai a tela passa a exigir sessao de master para criar contas. Assim nao
existe senha padrao de fabrica nem passo manual de instalacao -- e a janela
em que qualquer um poderia se cadastrar dura ate o primeiro cadastro.

Protecao das rotas: tudo exige sessao, menos o que esta em ROTAS_PUBLICAS.
E lista de permissao, nao de bloqueio: rota nova nasce protegida, e esquecer
de proteger deixa de ser possivel.
"""
import secrets
from functools import wraps

from flask import (Blueprint, abort, redirect, render_template, request,
                   session, url_for)

from . import config, contas

bp = Blueprint("acesso", __name__)

# Nomes de endpoint que dispensam sessao.
ROTAS_PUBLICAS = {
    "acesso.home", "acesso.entrar", "acesso.cadastrar", "acesso.recuperar",
    "static", "saude",
}

CHAVE_SESSAO = "sessao"
CHAVE_CSRF = "csrf"


# ------------------------------------------------------------ chave do cookie


def carregar_segredo():
    """Chave que assina o cookie de sessao, guardada em disco.

    Gerar a cada partida deslogaria todo mundo a cada reinicio do servico --
    e o servico reinicia sozinho depois de queda de luz e de atualizacao
    mensal. O arquivo fica fora do Git e so o dono le.
    """
    caminho = config.DIR_DADOS / "segredo.txt"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    if caminho.exists():
        valor = caminho.read_text(encoding="utf-8").strip()
        if valor:
            return valor
    valor = secrets.token_urlsafe(48)
    caminho.write_text(valor, encoding="utf-8")
    try:
        caminho.chmod(0o600)
    except OSError:
        pass  # Windows ignora; o arquivo ainda esta fora do Git
    return valor


# ------------------------------------------------------------ CSRF


def token_csrf():
    if CHAVE_CSRF not in session:
        session[CHAVE_CSRF] = secrets.token_urlsafe(32)
    return session[CHAVE_CSRF]


def conferir_csrf():
    """Sem isto, um site qualquer poderia postar em /cadastrar ou
    /trocar-senha usando o cookie de quem esta logado."""
    enviado = request.form.get("csrf", "")
    guardado = session.get(CHAVE_CSRF, "")
    if not guardado or not secrets.compare_digest(enviado, guardado):
        abort(400, "Formulario expirado. Recarregue a pagina e tente de novo.")


# ------------------------------------------------------------ sessao


def usuario_atual():
    token = session.get(CHAVE_SESSAO)
    if not token:
        return None
    u = contas.usuario_da_sessao(token)
    if u is None:
        session.pop(CHAVE_SESSAO, None)
    return u


def exigir_master(fn):
    @wraps(fn)
    def dentro(*a, **kw):
        u = usuario_atual()
        if not u or not u["e_master"]:
            abort(403)
        return fn(*a, **kw)
    return dentro


def _ip():
    """IP real de quem chamou.

    Atras do Cloudflare Tunnel toda requisicao chega de 127.0.0.1; o IP de
    verdade vem em CF-Connecting-IP. Sem ler esse cabecalho, o bloqueio por
    tentativa contaria o mundo inteiro como um IP so.
    """
    for cabecalho in ("CF-Connecting-IP", "X-Forwarded-For"):
        valor = request.headers.get(cabecalho)
        if valor:
            return valor.split(",")[0].strip()[:45]
    return (request.remote_addr or "")[:45]


def instalar(app):
    """Liga o controle de acesso na aplicacao."""
    app.secret_key = carregar_segredo()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,   # JavaScript nao le o cookie
        SESSION_COOKIE_SAMESITE="Lax",  # nao viaja em POST de outro site
        SESSION_COOKIE_SECURE=not app.debug,  # so por HTTPS em producao
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * contas.DIAS_SESSAO,
    )
    contas.criar_tabelas()
    app.register_blueprint(bp)

    @app.before_request
    def _porteiro():
        if request.endpoint in ROTAS_PUBLICAS or request.endpoint is None:
            return None
        u = usuario_atual()
        if not u:
            return redirect(url_for("acesso.entrar", proximo=request.full_path))
        # Senha temporaria: nao deixa navegar antes de trocar, senao a senha
        # de 24 h vira senha permanente na pratica.
        if u["trocar_senha"] and request.endpoint != "acesso.trocar_senha":
            return redirect(url_for("acesso.trocar_senha"))
        return None

    @app.context_processor
    def _ctx():
        return {"usuario": usuario_atual(), "csrf": token_csrf()}


# ------------------------------------------------------------ telas


@bp.route("/")
def home():
    if usuario_atual():
        return redirect(url_for("tela_busca"))
    return render_template(
        "home.html",
        primeiro_acesso=not contas.existe_algum_master(),
    )


@bp.route("/entrar", methods=["GET", "POST"])
def entrar():
    if usuario_atual():
        return redirect(url_for("tela_busca"))
    erro = None
    usuario = ""
    proximo = request.values.get("proximo") or ""

    if request.method == "POST":
        conferir_csrf()
        usuario = (request.form.get("usuario") or "").strip()
        try:
            token, u = contas.autenticar(
                usuario, request.form.get("senha") or "",
                ip=_ip(), agente=request.headers.get("User-Agent", ""),
            )
            session.permanent = True
            session[CHAVE_SESSAO] = token
            if u["trocar_senha"]:
                return redirect(url_for("acesso.trocar_senha"))
            # so aceita destino interno: "proximo" vem da URL e um endereco
            # de fora ali viraria redirecionamento aberto
            if proximo.startswith("/") and not proximo.startswith("//"):
                return redirect(proximo)
            return redirect(url_for("tela_busca"))
        except contas.ErroConta as e:
            erro = str(e)

    return render_template("entrar.html", erro=erro, usuario=usuario,
                           proximo=proximo)


@bp.route("/cadastrar", methods=["GET", "POST"])
def cadastrar():
    primeiro = not contas.existe_algum_master()
    atual = usuario_atual()
    pode = primeiro or (atual and atual["e_master"])

    if not pode:
        return render_template("cadastrar.html", bloqueado=True)

    erro = None
    dados = {"usuario": "", "nome": ""}
    if request.method == "POST":
        conferir_csrf()
        dados = {
            "usuario": (request.form.get("usuario") or "").strip(),
            "nome": (request.form.get("nome") or "").strip(),
        }
        senha = request.form.get("senha") or ""
        try:
            if senha != (request.form.get("senha2") or ""):
                raise contas.ErroConta("As duas senhas nao sao iguais.")
            uid, codigos = contas.criar_usuario(
                dados["usuario"], senha, nome=dados["nome"],
                e_master=primeiro,
                criado_por=(atual["usuario"] if atual else "primeiro acesso"),
            )
            # Os codigos so existem legiveis aqui. Passar pela sessao e o
            # jeito de mostra-los na proxima tela sem grava-los em lugar
            # nenhum -- e sao apagados assim que aparecem.
            session["codigos_novos"] = codigos
            session["codigos_de"] = dados["usuario"]
            if primeiro:
                token, _ = contas.autenticar(dados["usuario"], senha, ip=_ip())
                session.permanent = True
                session[CHAVE_SESSAO] = token
            return redirect(url_for("acesso.codigos"))
        except contas.ErroConta as e:
            erro = str(e)

    return render_template("cadastrar.html", erro=erro, dados=dados,
                           primeiro=primeiro, bloqueado=False)


@bp.route("/codigos")
def codigos():
    lista = session.pop("codigos_novos", None)
    de = session.pop("codigos_de", "")
    if not lista:
        return redirect(url_for("acesso.home"))
    return render_template("codigos.html", codigos=lista, de=de)


@bp.route("/recuperar", methods=["GET", "POST"])
def recuperar():
    erro = None
    ok = False
    restantes = None
    usuario = ""

    if request.method == "POST":
        conferir_csrf()
        usuario = (request.form.get("usuario") or "").strip()
        senha = request.form.get("senha") or ""
        try:
            if senha != (request.form.get("senha2") or ""):
                raise contas.ErroConta("As duas senhas nao sao iguais.")
            restantes = contas.recuperar_com_codigo(
                usuario, request.form.get("codigo") or "", senha, ip=_ip()
            )
            ok = True
        except contas.ErroConta as e:
            erro = str(e)

    return render_template("recuperar.html", erro=erro, ok=ok,
                           restantes=restantes, usuario=usuario)


@bp.route("/trocar-senha", methods=["GET", "POST"])
def trocar_senha():
    u = usuario_atual()
    if not u:
        return redirect(url_for("acesso.entrar"))
    erro = None

    if request.method == "POST":
        conferir_csrf()
        senha = request.form.get("senha") or ""
        try:
            if senha != (request.form.get("senha2") or ""):
                raise contas.ErroConta("As duas senhas nao sao iguais.")
            contas.trocar_senha(u["id"], senha)
            # trocar_senha derruba as sessoes, inclusive esta: entra de novo
            # com a senha nova para o usuario nao cair na tela de login
            token, _ = contas.autenticar(u["usuario"], senha, ip=_ip())
            session[CHAVE_SESSAO] = token
            return redirect(url_for("tela_busca"))
        except contas.ErroConta as e:
            erro = str(e)

    return render_template("trocar_senha.html", erro=erro,
                           obrigatorio=bool(u["trocar_senha"]))


@bp.route("/sair")
def sair():
    contas.encerrar_sessao(session.get(CHAVE_SESSAO))
    session.clear()
    return redirect(url_for("acesso.home"))
