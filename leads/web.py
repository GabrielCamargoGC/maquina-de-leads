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

from flask import (Flask, Response, redirect, render_template, request,
                   send_file, url_for)

from . import busca, config, estado, exportar, novidades

app = Flask(__name__)


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


@app.route("/")
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
    f = FormFiltros(request.form)
    formato = request.form.get("formato", "csv")
    fonte = request.form.get("fonte", "busca")
    if not f.preenchido:
        return redirect(url_for("tela_busca"))
    try:
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
    return app


def main():
    """Modo de desenvolvimento. Em producao quem serve e o waitress
    (ver servir.py), porque o servidor embutido do Flask nao aguenta 15
    pessoas e ele mesmo avisa isso no log."""
    criar_app()
    app.run(host="127.0.0.1", port=config.WEB_PORTA, debug=True)


if __name__ == "__main__":
    main()
