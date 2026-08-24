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
from werkzeug.middleware.proxy_fix import ProxyFix

from . import (atualizar_codigo, auditoria, busca, config, contas,
               estado, saude)

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
    /trocar-senha usando o cookie de quem esta logado.

    Quando NAO ha token guardado, a causa quase sempre nao e ataque nem
    formulario velho: e o cookie de sessao nao ter sido aceito. Ele e
    marcado Secure, entao o navegador simplesmente o descarta em HTTP puro,
    e a pessoa fica presa numa tela de "formulario expirado" que se repete
    para sempre por mais que ela recarregue. Vale distinguir os dois casos --
    o primeiro relato disso custou uma ida ao servidor para descobrir que
    faltava so um "s" no endereco.
    """
    enviado = request.form.get("csrf", "")
    guardado = session.get(CHAVE_CSRF, "")

    if not guardado:
        if not request.is_secure:
            abort(400, (
                "Este endereco foi aberto sem HTTPS, e por seguranca o "
                "navegador descarta o cookie de sessao em conexao nao "
                "segura. Abra o site com https:// no comeco do endereco."
            ))
        abort(400, (
            "A sessao do navegador nao foi aceita. Verifique se os cookies "
            "estao habilitados e tente de novo."
        ))

    if not secrets.compare_digest(enviado, guardado):
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
    # O cloudflared conversa com o site em HTTP puro no localhost, entao sem
    # isto o Flask acha que TODA requisicao e insegura -- mesmo a de quem
    # abriu o site em https. Quem sabe a verdade e o cabecalho
    # X-Forwarded-Proto que o tunel manda.
    #
    # Confiar nesse cabecalho so e seguro porque o site escuta em localhost e
    # o unico que fala com ele e o tunel. Exposto direto na rede, qualquer um
    # poderia mentir nele.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.secret_key = carregar_segredo()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,   # JavaScript nao le o cookie
        SESSION_COOKIE_SAMESITE="Lax",  # nao viaja em POST de outro site
        SESSION_COOKIE_SECURE=not app.debug,  # so por HTTPS em producao
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * contas.DIAS_SESSAO,
    )
    contas.criar_tabelas()
    auditoria.criar_tabelas()
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

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def _erro(e):
        # A tela padrao do Flask e um HTML cru sem estilo. Quem cai nela ja
        # esta com um problema; nao precisa achar que o site quebrou de vez.
        return render_template("erro.html", codigo=e.code, nome=e.name,
                               mensagem=e.description), e.code


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
            auditoria.registrar(auditoria.LOGIN_OK, usuario=u["usuario"], ip=_ip())
            if u["trocar_senha"]:
                return redirect(url_for("acesso.trocar_senha"))
            # so aceita destino interno: "proximo" vem da URL e um endereco
            # de fora ali viraria redirecionamento aberto
            if proximo.startswith("/") and not proximo.startswith("//"):
                return redirect(proximo)
            return redirect(url_for("tela_busca"))
        except contas.ErroConta as e:
            erro = str(e)
            # "Muitas tentativas" e um evento diferente de senha errada: um
            # e alguem digitando errado, o outro e o freio ja atuando.
            tipo = (auditoria.LOGIN_BLOQUEADO if "tentativas" in erro.lower()
                    else auditoria.LOGIN_ERRO)
            auditoria.registrar(tipo, usuario=usuario, ip=_ip(), motivo=erro)

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
            auditoria.registrar(
                auditoria.CONTA_CRIADA,
                usuario=(atual["usuario"] if atual else dados["usuario"]),
                ip=_ip(), alvo=dados["usuario"], master=primeiro,
            )
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
            auditoria.registrar(auditoria.CODIGO_USADO, usuario=usuario,
                                ip=_ip(), restantes=restantes)
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
            auditoria.registrar(auditoria.SENHA_TROCADA, usuario=u["usuario"],
                                ip=_ip())
            token, _ = contas.autenticar(u["usuario"], senha, ip=_ip())
            session[CHAVE_SESSAO] = token
            return redirect(url_for("tela_busca"))
        except contas.ErroConta as e:
            erro = str(e)

    return render_template("trocar_senha.html", erro=erro,
                           obrigatorio=bool(u["trocar_senha"]))


@bp.route("/sair")
def sair():
    u = usuario_atual()
    if u:
        auditoria.registrar(auditoria.SAIU, usuario=u["usuario"], ip=_ip())
    contas.encerrar_sessao(session.get(CHAVE_SESSAO))
    session.clear()
    return redirect(url_for("acesso.home"))


# ------------------------------------------------------------ painel master


@bp.route("/master")
@exigir_master
def master():
    return render_template(
        "master.html",
        pagina="master",
        maquina=saude.coletar(),
        codigo_situacao=atualizar_codigo.situacao(),
        usuarios=contas.listar_usuarios(),
        eventos=auditoria.listar(limite=60),
        resumo=auditoria.resumo(horas=24),
        rotulos=auditoria.ROTULOS,
        base_pronta=busca.base_pronta(),
        info_base=estado.ler(),
        codigo=session.pop("codigo_resultado", None),
        senha_gerada=session.pop("senha_gerada", None),
        codigos_gerados=session.pop("codigos_gerados", None),
        aviso=session.pop("aviso_master", None),
    )


@bp.route("/master/codigo", methods=["POST"])
@exigir_master
def master_codigo():
    """Ver e aplicar atualizacao do codigo, sem teclado na maquina.

    Dois passos separados: primeiro mostra o que viria, depois aplica. Um
    botao so, que baixa e reinicia de uma vez, seria atualizar as cegas um
    servidor que ninguem consegue ver.
    """
    conferir_csrf()
    eu = usuario_atual()
    acao = request.form.get("acao")

    if acao == "conferir":
        session["codigo_resultado"] = {"tipo": "conferido", **atualizar_codigo.conferir()}
        return redirect(url_for("acesso.master"))

    if acao == "aplicar":
        ok, saida = atualizar_codigo.aplicar()
        resultado = {"tipo": "aplicado", "ok": ok, "saida": saida}
        if ok:
            ok_dep, saida_dep = atualizar_codigo.instalar_dependencias()
            resultado["dependencias"] = saida_dep if not ok_dep else None
            ok_r, msg_r = atualizar_codigo.reiniciar_servico()
            resultado["reinicio"] = msg_r
            resultado["reinicio_ok"] = ok_r
        auditoria.registrar(auditoria.CODIGO_ATUALIZADO, usuario=eu["usuario"],
                            ip=_ip(), ok=ok, saida=saida[:300])
        session["codigo_resultado"] = resultado
        return redirect(url_for("acesso.master"))

    return redirect(url_for("acesso.master"))


@bp.route("/master/acao", methods=["POST"])
@exigir_master
def master_acao():
    conferir_csrf()
    eu = usuario_atual()
    acao = request.form.get("acao")
    alvo_id = request.form.get("usuario_id", type=int)
    alvo = contas.buscar_usuario(usuario_id=alvo_id) if alvo_id else None

    if not alvo:
        session["aviso_master"] = "Usuario nao encontrado."
        return redirect(url_for("acesso.master"))

    # O master nao pode se desativar nem se trancar fora: sem isto, um clique
    # errado deixaria a maquina sem ninguem capaz de administrar contas.
    if alvo["id"] == eu["id"] and acao in ("desativar", "senha_temporaria"):
        session["aviso_master"] = "Voce nao pode fazer isso na sua propria conta."
        return redirect(url_for("acesso.master"))

    if acao == "desativar":
        contas.definir_ativo(alvo["id"], False)
        auditoria.registrar(auditoria.CONTA_DESATIVADA, usuario=eu["usuario"],
                            ip=_ip(), alvo=alvo["usuario"])
        session["aviso_master"] = f"Conta '{alvo['usuario']}' desativada."

    elif acao == "ativar":
        contas.definir_ativo(alvo["id"], True)
        auditoria.registrar(auditoria.CONTA_ATIVADA, usuario=eu["usuario"],
                            ip=_ip(), alvo=alvo["usuario"])
        session["aviso_master"] = f"Conta '{alvo['usuario']}' reativada."

    elif acao == "senha_temporaria":
        senha = contas.gerar_senha_temporaria(alvo["id"])
        auditoria.registrar(auditoria.SENHA_TEMPORARIA, usuario=eu["usuario"],
                            ip=_ip(), alvo=alvo["usuario"])
        session["senha_gerada"] = {"usuario": alvo["usuario"], "senha": senha}

    elif acao == "refazer_codigos":
        codigos = contas.gerar_codigos(alvo["id"])
        auditoria.registrar(auditoria.CODIGOS_REFEITOS, usuario=eu["usuario"],
                            ip=_ip(), alvo=alvo["usuario"])
        session["codigos_gerados"] = {"usuario": alvo["usuario"], "codigos": codigos}

    return redirect(url_for("acesso.master"))
