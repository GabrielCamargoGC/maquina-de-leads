#!/usr/bin/env python3
r"""
Site interno de busca de leads.

Diferencas de fundo em relacao ao app.py antigo, que era feito para uma
pessoa num Mac:

  - sem estado global de processo. O antigo guardava o andamento do download
    num dict do modulo, o que da errado assim que existe mais de um worker.
  - export nao acontece dentro do request. Uma cidade grande passa de 200 mil
    linhas; gerar isso na resposta estoura o tempo do navegador e prende uma
    thread do servidor por minutos. Aqui vira tarefa em fila.
  - a busca nao chama sys.exit. O antigo encerrava o processo para reclamar
    de cidade errada; aqui e excecao tratada, com sugestao na tela.
  - nada de multiprocessing por requisicao: uma busca de um usuario nao pode
    consumir a maquina dos outros 14.

Servido por waitress em producao (gunicorn nao roda no Windows).
"""
import time
import traceback
from pathlib import Path

from urllib.parse import quote

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, url_for)

from . import (acesso, auditoria, busca, config, consulta, estado,
               exportar, novidades, refinar)

app = Flask(__name__)


@app.template_filter("data")
def _data(d):
    """2019-02-18 -> 18/02/2019. Data em formato ISO na tela faz o leitor
    brasileiro parar para interpretar; o formato daqui ele le sem pensar."""
    if not d:
        return "—"
    try:
        return d.strftime("%d/%m/%Y")
    except AttributeError:
        texto = str(d)[:10]
        if len(texto) == 10 and texto[4] == "-":
            return f"{texto[8:10]}/{texto[5:7]}/{texto[0:4]}"
        return texto


@app.template_filter("telefone_incompleto")
def _telefone_incompleto(numero):
    """True para celular em formato pre-2012 (8 digitos comecando com 8/9).

    Mostrar sem avisar faz a pessoa discar, nao completar, e concluir que o
    dado esta errado -- quando na verdade e antigo.
    """
    return consulta.celular_antigo("", numero or "")


@app.template_filter("milhar")
def _milhar(n):
    """1234567 -> 1.234.567 (separador brasileiro)."""
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", ".")


# O sabre, desenhado direto no HTML. Sem arquivo .ico, sem uma requisicao a
# mais por pagina, e sem o 404 de favicon que suja o console do navegador.
FAVICON = quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect x='14' y='3' width='4' height='26' rx='2' fill='#FF6B00'/>"
    "<rect x='13' y='21' width='6' height='8' rx='2' fill='#8A8A8F'/>"
    "</svg>"
)


@app.context_processor
def _globais():
    return {"favicon": FAVICON}


# CSS e JS saem com a versao na URL: /static/sabre.css?v=68b5c2a1
#
# Sem isto, arquivo que ja existia continua vindo do cache depois de uma
# atualizacao -- o navegador de quem ja usava o site, e principalmente o
# Cloudflare, que guarda .css e .js por extensao. Deu exatamente nisso na
# primeira vez: o cidades.js novo carregou (arquivo inedito, cache nenhum) e
# o sabre.css veio velho, entao a tela montou o campo de cidades sem nenhum
# estilo. Pedir Ctrl+F5 para 15 pessoas a cada atualizacao nao e solucao.
#
# A versao e o mtime do arquivo. git pull mexe no mtime, e o servico
# reinicia logo depois, entao o valor se renova sozinho -- ninguem precisa
# lembrar de incrementar nada.
_versoes_estaticas = {}


@app.url_defaults
def _versionar_estatico(endpoint, valores):
    if endpoint != "static" or "filename" not in valores:
        return
    nome = valores["filename"]
    if nome not in _versoes_estaticas:
        try:
            caminho = Path(app.static_folder) / nome
            _versoes_estaticas[nome] = format(int(caminho.stat().st_mtime), "x")
        except OSError:
            _versoes_estaticas[nome] = "0"   # arquivo sumiu: nao quebra a pagina
    valores["v"] = _versoes_estaticas[nome]


# Com a versao na URL, o arquivo pode ser guardado para sempre: quando muda,
# muda tambem o endereco, e o cache antigo simplesmente deixa de ser pedido.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000


# Liga contas, sessao e o porteiro que exige login. Fica aqui, logo apos
# criar o app, para que nenhuma rota registrada abaixo escape da protecao.
acesso.instalar(app)


# ------------------------------------------------------------ apoio


class FormFiltros:
    """Espelho do formulario: guarda o que o usuario digitou (para redesenhar
    a tela com os campos preenchidos) e sabe virar um busca.Filtros."""

    def __init__(self, args):
        self.cidades_txt = (args.get("cidades") or "").strip()
        self.cidades = [c.strip() for c in self.cidades_txt.split(",") if c.strip()]
        self.uf = (args.get("uf") or "").strip().upper() or None
        self.bairro = (args.get("bairro") or "").strip() or None
        self.cnae = (args.get("cnae") or "").strip() or None
        self.com_telefone = args.get("com_telefone") in ("1", "on", "true")
        self.com_email = args.get("com_email") in ("1", "on", "true")
        self.apenas_simples = args.get("apenas_simples") in ("1", "on", "true")
        self.apenas_mei = args.get("apenas_mei") in ("1", "on", "true")
        self.apenas_matriz = args.get("apenas_matriz") in ("1", "on", "true")
        self.apenas_ativas = args.get("todas_situacoes") not in ("1", "on", "true")
        porte = (args.get("porte") or "").strip()
        self.portes = [porte] if porte else []
        self.capital_min = self._numero(args.get("capital_min"))
        self.aberta_de = (args.get("aberta_de") or "").strip() or None
        self.aberta_ate = (args.get("aberta_ate") or "").strip() or None

    @staticmethod
    def _numero(v):
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    @property
    def tem_avancado(self):
        """Abre o bloco "Mais filtros" ja expandido quando algo dentro dele
        esta em uso -- senao o usuario nao ve por que a busca filtrou."""
        return bool(self.portes or self.capital_min or self.aberta_de
                    or self.aberta_ate or self.apenas_matriz
                    or not self.apenas_ativas)

    @property
    def preenchido(self):
        return bool(self.cidades or self.uf)

    def para_busca(self):
        return busca.Filtros(
            cidades=self.cidades, uf=self.uf, bairro=self.bairro, cnae=self.cnae,
            apenas_ativas=self.apenas_ativas, apenas_simples=self.apenas_simples,
            apenas_mei=self.apenas_mei, com_telefone=self.com_telefone,
            com_email=self.com_email, portes=self.portes,
            capital_min=self.capital_min, aberta_de=self.aberta_de,
            aberta_ate=self.aberta_ate, apenas_matriz=self.apenas_matriz,
        )

    def query(self):
        """Os mesmos campos de volta como querystring, para os botoes de
        export repetirem exatamente a busca que esta na tela."""
        d = {"cidades": self.cidades_txt}
        if self.uf:
            d["uf"] = self.uf
        if self.bairro:
            d["bairro"] = self.bairro
        if self.cnae:
            d["cnae"] = self.cnae
        for nome in ("com_telefone", "com_email", "apenas_simples", "apenas_mei",
                     "apenas_matriz"):
            if getattr(self, nome):
                d[nome] = "1"
        if not self.apenas_ativas:
            d["todas_situacoes"] = "1"
        if self.portes:
            d["porte"] = self.portes[0]
        if self.capital_min is not None:
            d["capital_min"] = self.capital_min
        if self.aberta_de:
            d["aberta_de"] = self.aberta_de
        if self.aberta_ate:
            d["aberta_ate"] = self.aberta_ate
        return d

    def descricao(self):
        partes = [self.cidades_txt or self.uf or "?"]
        if self.bairro:
            partes.append(self.bairro)
        if self.cnae:
            partes.append(self.cnae)
        return " / ".join(partes)


def _comum(pagina):
    return {
        "pagina": pagina,
        "base_pronta": busca.base_pronta(),
        "info_base": estado.ler(),
    }


# ------------------------------------------------------------ telas


# Lista de municipios, servida uma vez por versao da base.
#
# Montar isto a cada requisicao seria desperdicio: a base so muda uma vez por
# mes, e a resposta e identica entre uma troca e outra. A chave do cache e a
# referencia da base, entao a troca mensal invalida sozinha.
_cidades_cache = {}


@app.route("/api/cidades")
def api_cidades():
    if not busca.base_pronta():
        return jsonify({"ref": "", "cidades": []})

    ref = str(estado.ler().get("referencia") or "sem-ref")
    if ref not in _cidades_cache:
        _cidades_cache.clear()          # so a versao corrente interessa
        _cidades_cache[ref] = [
            [nome, uf, int(qtd or 0)] for nome, uf, qtd in busca.listar_cidades()
        ]
    lista = _cidades_cache[ref]

    etiqueta = f'W/"cidades-{ref}-{len(lista)}"'
    if request.headers.get("If-None-Match") == etiqueta:
        return Response(status=304, headers={"ETag": etiqueta})

    r = jsonify({"ref": ref, "cidades": lista})
    # private: e conteudo de usuario logado, o Cloudflare nao deve guardar
    # copia compartilhada.
    r.headers["Cache-Control"] = "private, max-age=86400"
    r.headers["ETag"] = etiqueta
    return r


@app.route("/busca")
def tela_busca():
    f = FormFiltros(request.args)
    ctx = dict(_comum("busca"), f=f, query=f.query(), erro=None,
               total=None, linhas=[], segundos=0.0)

    if f.preenchido and ctx["base_pronta"]:
        t0 = time.time()
        try:
            filtros = f.para_busca()
            ctx["total"] = busca.contar(filtros)
            ctx["linhas"] = busca.buscar(filtros, limite=config.MAX_LINHAS_TELA)
            ctx["segundos"] = time.time() - t0
        except busca.ErroBusca as e:
            ctx["erro"] = str(e)
        except Exception as e:
            traceback.print_exc()
            ctx["erro"] = f"Erro inesperado na busca: {e}"
    return render_template("busca.html", **ctx)


@app.route("/consulta")
def tela_consulta():
    termo = (request.args.get("termo") or "").strip()
    amplo = request.args.get("amplo") in ("1", "on", "true")
    ctx = dict(_comum("consulta"), termo=termo, total=None, linhas=[],
               tipo=None, descricao=None, aviso=None, segundos=0.0, amplo=amplo)

    if termo and ctx["base_pronta"]:
        t0 = time.time()
        try:
            linhas, tipo, aviso = consulta.procurar(termo, amplo=amplo)
            ctx.update(linhas=linhas, total=len(linhas), tipo=tipo,
                       descricao=consulta.DESCRICAO.get(tipo), aviso=aviso,
                       segundos=time.time() - t0)
        except Exception as e:
            traceback.print_exc()
            ctx["aviso"] = f"Erro inesperado na consulta: {e}"
    return render_template("consulta.html", **ctx)


@app.route("/empresa/<cnpj>")
def tela_empresa(cnpj):
    e = consulta.uma(cnpj)
    if not e:
        return render_template("erro.html", codigo=404, nome="Empresa nao encontrada",
                               mensagem="Nenhum estabelecimento com esse CNPJ na base "
                                        "atual.", **_comum("consulta")), 404
    return render_template(
        "empresa.html", **_comum("consulta"), e=e,
        secundarios=consulta.cnaes_secundarios(e.get("cnae_secundaria")),
        irmas=consulta.irmas(e.get("cnpj_basico"), e.get("cnpj_numerico")),
    )


MAX_UPLOAD_MB = 50
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


class FormRefinar:
    """Espelho do formulario de Refinar, para redesenhar a tela preenchida."""

    def __init__(self, form):
        self.portes = form.getlist("porte")
        self.situacoes = form.getlist("situacao")
        self.simples = (form.get("simples") or "").strip()
        self.mei = (form.get("mei") or "").strip()
        self.com_telefone = form.get("com_telefone") in ("1", "on", "true")
        self.com_email = form.get("com_email") in ("1", "on", "true")
        self.so_matriz = form.get("so_matriz") in ("1", "on", "true")
        self.uf_txt = (form.get("uf") or "").strip()
        self.ufs = [u.strip().upper() for u in self.uf_txt.split(",") if u.strip()]
        self.cidade = (form.get("cidade") or "").strip()
        self.bairro = (form.get("bairro") or "").strip()
        self.cnae = (form.get("cnae") or "").strip()
        self.natureza = (form.get("natureza") or "").strip()
        self.capital_min = FormFiltros._numero(form.get("capital_min"))
        self.aberta_de = (form.get("aberta_de") or "").strip()
        self.aberta_ate = (form.get("aberta_ate") or "").strip()
        self.situacao_de = (form.get("situacao_de") or "").strip()
        self.situacao_ate = (form.get("situacao_ate") or "").strip()

    @property
    def tem_avancado(self):
        return bool(self.ufs or self.cidade or self.bairro or self.cnae
                    or self.natureza or self.capital_min is not None
                    or self.aberta_de or self.aberta_ate
                    or self.situacao_de or self.situacao_ate)

    def como_dict(self):
        return {
            "portes": self.portes, "situacoes": self.situacoes,
            "simples": self.simples, "mei": self.mei,
            "com_telefone": self.com_telefone, "com_email": self.com_email,
            "so_matriz": self.so_matriz, "ufs": self.ufs,
            "cidade": self.cidade, "bairro": self.bairro, "cnae": self.cnae,
            "natureza": self.natureza, "capital_min": self.capital_min,
            "aberta_de": self.aberta_de, "aberta_ate": self.aberta_ate,
            "situacao_de": self.situacao_de, "situacao_ate": self.situacao_ate,
        }


@app.route("/refinar", methods=["GET", "POST"])
def tela_refinar():
    # request.form vem vazio no GET; a mesma classe serve para as duas
    # passagens, e a tela sai com os campos como o usuario deixou.
    f = FormRefinar(request.form)
    ctx = dict(_comum("refinar"), f=f, erro=None, resultado=None,
               colunas_previa=[], faltando=[], segundos=0.0,
               limite_mb=MAX_UPLOAD_MB)

    if request.method == "POST":
        acesso.conferir_csrf()
        enviado = request.files.get("arquivo")
        if not enviado or not enviado.filename:
            ctx["erro"] = "Escolha um arquivo."
            return render_template("refinar.html", **ctx)

        sufixo = Path(enviado.filename).suffix.lower()
        if sufixo not in (".xlsx", ".csv"):
            ctx["erro"] = "Envie um arquivo .xlsx ou .csv."
            return render_template("refinar.html", **ctx)

        # A planilha vai para um arquivo temporario e some ao terminar: e
        # lista de contato de cliente, nao tem por que ficar no servidor.
        import tempfile
        temporario = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=sufixo) as tmp:
                enviado.save(tmp)
                temporario = Path(tmp.name)

            t0 = time.time()
            info = refinar.inspecionar(temporario)
            resultado = refinar.refinar(temporario, f.como_dict(), info["internas"])
            ctx["segundos"] = time.time() - t0
            ctx["resultado"] = resultado
            if resultado["previa"]:
                ctx["colunas_previa"] = list(resultado["previa"][0])[:8]

            rotulos_ausentes = [
                exportar.ROTULOS[i] for i in ("natureza_juridica", "data_situacao")
                if i not in info["internas"]
            ]
            ctx["faltando"] = rotulos_ausentes

            auditoria.registrar(
                auditoria.REFINOU,
                usuario=(acesso.usuario_atual() or {}).get("usuario", ""),
                ip=acesso._ip(), entraram=resultado["total"],
                sairam=resultado["sobraram"], filtros=resultado["condicoes"],
            )
        except refinar.ErroPlanilha as e:
            ctx["erro"] = str(e)
        except Exception as e:
            traceback.print_exc()
            ctx["erro"] = f"Erro inesperado ao ler a planilha: {e}"
        finally:
            if temporario:
                temporario.unlink(missing_ok=True)

    return render_template("refinar.html", **ctx)


@app.errorhandler(413)
def _grande_demais(e):
    """Sem isto, a planilha acima do limite cai na pagina crua do Flask, em
    ingles e sem dizer qual e o limite."""
    return render_template(
        "erro.html", codigo=413, nome="Arquivo grande demais",
        mensagem=(f"O envio passa de {MAX_UPLOAD_MB} MB. Exporte a planilha "
                  f"em partes menores e refine uma de cada vez."),
        **_comum("refinar")), 413


@app.route("/refinar/<ident>/baixar/<formato>")
def baixar_refinado(ident, formato):
    if formato not in ("xlsx", "csv"):
        return redirect(url_for("tela_refinar"))
    try:
        caminho = refinar.escrever(ident, formato)
    except refinar.ErroPlanilha as e:
        return render_template("erro.html", codigo=404, nome="Resultado expirado",
                               mensagem=str(e), **_comum("refinar")), 404
    return send_file(caminho, as_attachment=True,
                     download_name=f"refinado.{formato}")


@app.route("/novidades")
def tela_novidades():
    f = FormFiltros(request.args)
    ctx = dict(_comum("novidades"), f=f, query=f.query(), erro=None,
               total=None, linhas=[], aproximado=False)

    if f.preenchido and ctx["base_pronta"]:
        try:
            filtros = f.para_busca()
            ctx["total"], ctx["aproximado"] = novidades.contar_novas(filtros)
            ctx["linhas"], _ = novidades.buscar_novas(
                filtros, limite=config.MAX_LINHAS_TELA
            )
        except busca.ErroBusca as e:
            ctx["erro"] = str(e)
        except Exception as e:
            traceback.print_exc()
            ctx["erro"] = f"Erro inesperado: {e}"
    return render_template("novidades.html", **ctx)


@app.route("/exportar", methods=["POST"])
def pedir_export():
    # Export e POST autenticado: sem conferir o token, outro site poderia
    # disparar exportacoes usando o cookie de quem esta logado.
    acesso.conferir_csrf()
    f = FormFiltros(request.form)
    formato = request.form.get("formato", "csv")
    fonte = request.form.get("fonte", "busca")
    if not f.preenchido:
        return redirect(url_for("tela_busca"))
    try:
        auditoria.registrar(
            auditoria.EXPORTOU,
            usuario=(acesso.usuario_atual() or {}).get("usuario", ""),
            ip=acesso._ip(), filtros=f.descricao(), formato=formato, fonte=fonte,
        )
        job_id = exportar.enfileirar(
            f.para_busca(), formato=formato,
            descricao=f"{f.descricao()} ({'novas' if fonte == 'novidades' else 'busca'})",
            fonte=fonte,
        )
    except ValueError as e:
        return Response(str(e), status=400, mimetype="text/plain")
    return redirect(url_for("tela_exports", novo=job_id))


@app.route("/exports")
def tela_exports():
    jobs = exportar.listar_jobs()
    return render_template(
        "exports.html", **_comum("exports"), jobs=jobs,
        destaque=request.args.get("novo"),
        tem_pendente=any(j["estado"] in ("na_fila", "rodando") for j in jobs),
    )


@app.route("/exports/<job_id>/baixar")
def baixar_export(job_id):
    job = exportar.ver_job(job_id)
    if not job or job["estado"] != "pronto" or not job["arquivo"]:
        return Response("Arquivo nao esta pronto.", status=404, mimetype="text/plain")
    caminho = Path(job["arquivo"])
    if not caminho.exists():
        return Response(
            "O arquivo expirou (ficam 7 dias). Faca o pedido de novo.",
            status=410, mimetype="text/plain",
        )
    nome = (job["descricao"] or "leads").replace("/", "-").replace(" ", "_")
    return send_file(caminho, as_attachment=True,
                     download_name=f"{nome}.{job['formato']}")


@app.route("/status")
def tela_status():
    info = estado.ler()
    itens = [
        ("Mes da Receita", info.get("referencia") or "desconhecido"),
        ("Atualizada em", info.get("atualizada_em") or "-"),
        ("Estabelecimentos", _milhar(info.get("linhas"))),
        ("Municipios", _milhar(info.get("municipios"))),
        ("Origem", "arquivos.receitafederal.gov.br (Dados Abertos do CNPJ)"),
        ("Pasta dos dados", str(config.DIR_ATUAL)),
        ("Comparacao de novidades",
         "exata (ha base anterior)" if novidades.tem_base_anterior()
         else "aproximada (ainda sem base anterior)"),
    ]

    ufs = []
    if busca.base_pronta():
        try:
            cam = busca._caminhos()
            ufs = busca.conexao().cursor().execute(
                f"SELECT uf, count(*), sum(qtd_empresas) "
                f"FROM read_parquet('{cam['cidades']}') GROUP BY uf ORDER BY uf"
            ).fetchall()
        except Exception:
            traceback.print_exc()
    return render_template("status.html", **_comum("status"), itens=itens, ufs=ufs)


@app.route("/saude")
def saude():
    """Usado pelo monitoramento e pelo instalador para saber se subiu."""
    ok = busca.base_pronta()
    return {"ok": ok, "base": estado.ler().get("referencia")}, (200 if ok else 503)


def criar_app():
    config.garantir_pastas()
    exportar.iniciar_workers()
    exportar.iniciar_faxina()
    return app


def main():
    """Modo de desenvolvimento. Em producao quem serve e o waitress
    (ver servir.py), porque o servidor embutido do Flask nao aguenta 15
    pessoas e ele mesmo avisa isso no log."""
    criar_app()
    app.run(host="127.0.0.1", port=config.WEB_PORTA, debug=True)


if __name__ == "__main__":
    main()
