#!/usr/bin/env python3
"""
Interface simples (pagina no navegador) para nao precisar digitar comando
nenhum no dia a dia. Abra com o arquivo abrir_app.command (Mac).

Roda um servidor local (so no seu computador, ninguem de fora acessa) na
porta 5000 e abre o navegador sozinho em http://127.0.0.1:5000
"""
import csv
import io
import os
import threading
import time
import traceback
import webbrowser
from urllib.parse import urljoin

from flask import Flask, Response, redirect, render_template_string, request

import baixar_arquivos as ba
import buscar_leads as bl
import monitorar_novos as mn
import config

app = Flask(__name__)

status = {"baixando": False, "mensagem": "", "erro": None}

MAX_LINHAS_TABELA = 300
HISTORICO_WEB_DIR = "historico_web"

ESTILO = """
<style>
  :root {
    --accent: #2952e3;
    --accent-dark: #1c3aa8;
    --accent-bg: #eef1fd;
    --bg: #f6f7fb;
    --card: #ffffff;
    --border: #e6e8f0;
    --text: #1c1f2b;
    --text-muted: #6b7280;
    --green-bg: #e7f7ee;
    --green-text: #1a7f4e;
    --gray-bg: #f1f2f5;
    --gray-text: #6b7280;
    --amber-bg: #fff6e0;
    --amber-text: #92600a;
    --red-bg: #fdeceb;
    --red-text: #b13a2f;
    --radius: 12px;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 40px 20px 60px;
  }
  .container { max-width: 940px; margin: 0 auto; }
  .topo { margin-bottom: 24px; display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 8px; }
  .topo h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
  .topo p { font-size: 14px; color: var(--text-muted); margin: 0; }
  .nav-link { font-size: 13px; color: var(--accent); text-decoration: none; font-weight: 600; }
  .nav-link:hover { text-decoration: underline; }

  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 24px;
    margin-bottom: 20px;
    box-shadow: 0 1px 2px rgba(20,20,43,0.04);
  }

  .alerta { border-radius: 8px; padding: 10px 14px; font-size: 13px; margin-top: 12px; display: flex; gap: 8px; align-items: flex-start; }
  .alerta-info { background: var(--accent-bg); color: var(--accent-dark); }
  .alerta-ok { background: var(--green-bg); color: var(--green-text); }
  .alerta-erro { background: var(--red-bg); color: var(--red-text); white-space: pre-wrap; }
  .alerta-aviso { background: var(--amber-bg); color: var(--amber-text); }

  .linha-form { display: flex; gap: 16px; flex-wrap: wrap; }
  .campo { display: flex; flex-direction: column; gap: 4px; flex: 1; min-width: 140px; }
  .campo label { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.03em; }
  .campo input[type=text] {
    padding: 10px 12px; font-size: 14px; border: 1px solid var(--border);
    border-radius: 8px; background: #fbfbfd; transition: border-color .15s;
  }
  .campo input[type=text]:focus { outline: none; border-color: var(--accent); background: #fff; }
  .campo.pequeno { max-width: 90px; }

  .checks { display: flex; gap: 20px; margin-top: 16px; align-items: center; flex-wrap: wrap; }
  .toggle { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text); cursor: pointer; }
  .toggle input { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }

  button.primario, button.secundario {
    font-size: 14px; font-weight: 600; padding: 10px 20px; border-radius: 8px;
    cursor: pointer; border: none; margin-top: 18px;
  }
  button.primario { background: var(--accent); color: #fff; }
  button.primario:hover { background: var(--accent-dark); }
  button.secundario { background: var(--accent-bg); color: var(--accent-dark); border: 1px solid var(--border); }
  button.secundario:hover { background: #e2e6fb; }
  button:disabled { opacity: .6; cursor: not-allowed; }

  .resumo { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
  .resumo .num { font-size: 20px; font-weight: 700; }
  .resumo .label { font-size: 13px; color: var(--text-muted); }
  a.btn-csv {
    font-size: 13px; font-weight: 600; color: #fff; background: var(--accent);
    padding: 8px 16px; border-radius: 8px; text-decoration: none;
  }
  a.btn-csv:hover { background: var(--accent-dark); }

  table { border-collapse: collapse; width: 100%; margin-top: 4px; font-size: 13px; }
  thead th {
    text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em;
    color: var(--text-muted); border-bottom: 1px solid var(--border); padding: 8px 10px;
  }
  tbody td { padding: 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  tbody tr:hover { background: #fafbff; }
  tbody tr:last-child td { border-bottom: none; }

  .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .pill-sim { background: var(--green-bg); color: var(--green-text); }
  .pill-nao { background: var(--gray-bg); color: var(--gray-text); }
  .pill-novo { background: var(--accent-bg); color: var(--accent-dark); }
  .cnae-badge { font-size: 11px; color: var(--text-muted); }
  .razao { font-weight: 600; }
  .fantasia { font-size: 12px; color: var(--text-muted); }
  a.contato { color: var(--accent); text-decoration: none; font-size: 12.5px; }
  a.contato:hover { text-decoration: underline; }
</style>
"""

PAGINA = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>Leads CNPJ</title>
{% if status_baixando %}<meta http-equiv="refresh" content="10">{% endif %}
""" + ESTILO + """
</head>
<body>
<div class="container">

  <div class="topo">
    <div>
      <h1>Leads CNPJ</h1>
      <p>Busca de empresas por cidade, bairro, CNAE e Simples/MEI — dados abertos da Receita Federal.</p>
    </div>
    <a class="nav-link" href="/novidades">Ver empresas novas por município →</a>
  </div>

  <div class="card">
    <form method="get" action="/baixar" style="display:inline">
      <button type="submit" class="secundario" {% if status_baixando %}disabled{% endif %}>
        {% if status_baixando %}Baixando, aguarde...{% else %}Atualizar base da Receita{% endif %}
      </button>
    </form>
    {% if status_msg %}
      <div class="alerta {{ 'alerta-erro' if status_erro else 'alerta-ok' }}">{{ status_msg }}</div>
    {% else %}
      <div class="alerta alerta-info">Atualize 1x por mês — a Receita só libera dados novos nessa cadência. Pode levar 30-60 min.</div>
    {% endif %}
  </div>

  <div class="card">
    <form method="get" action="/">
      <div class="linha-form">
        <div class="campo">
          <label>Cidade</label>
          <input type="text" name="cidade" value="{{ cidade or '' }}" placeholder="Ex.: São Paulo" required>
        </div>
        <div class="campo pequeno">
          <label>UF</label>
          <input type="text" name="uf" value="{{ uf or '' }}" maxlength="2" placeholder="SP">
        </div>
        <div class="campo">
          <label>Bairro (opcional)</label>
          <input type="text" name="bairro" value="{{ bairro or '' }}" placeholder="Cidade toda se em branco">
        </div>
        <div class="campo">
          <label>CNAE ou ramo</label>
          <input type="text" name="cnae" value="{{ cnae or '' }}" placeholder="Ex.: contabilidade">
        </div>
      </div>
      <div class="checks">
        <label class="toggle"><input type="checkbox" name="apenas_simples" value="1" {% if apenas_simples %}checked{% endif %}> Só Simples Nacional</label>
        <label class="toggle"><input type="checkbox" name="apenas_mei" value="1" {% if apenas_mei %}checked{% endif %}> Só MEI</label>
      </div>
      <button type="submit" class="primario">Buscar</button>
    </form>
  </div>

  {% if erro_busca %}
    <div class="card">
      <div class="alerta alerta-erro">{{ erro_busca }}</div>
    </div>
  {% endif %}

  {% if linhas is not none %}
    <div class="card">
      <div class="resumo">
        <div>
          <div class="num">{{ linhas|length }}</div>
          <div class="label">empresa(s) encontrada(s)</div>
        </div>
        {% if linhas %}
          <a class="btn-csv" href="/csv?cidade={{ cidade }}&uf={{ uf }}&bairro={{ bairro or '' }}&cnae={{ cnae or '' }}&apenas_simples={{ 1 if apenas_simples else '' }}&apenas_mei={{ 1 if apenas_mei else '' }}">Baixar CSV</a>
        {% endif %}
      </div>
      {% if linhas|length > max_linhas %}
        <div class="alerta alerta-aviso" style="margin-top:14px">Mostrando as primeiras {{ max_linhas }} de {{ linhas|length }} na tela — o CSV traz todas.</div>
      {% endif %}
    </div>

    {% if linhas %}
    <div class="card" style="padding:0; overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Empresa</th><th>CNAE</th><th>Simples</th><th>MEI</th><th>Contato</th><th>Endereço</th>
          </tr>
        </thead>
        <tbody>
        {% for l in linhas[:max_linhas] %}
          <tr>
            <td>
              <div class="razao">{{ l.razao_social }}</div>
              {% if l.nome_fantasia %}<div class="fantasia">{{ l.nome_fantasia }}</div>{% endif %}
              <div class="fantasia">{{ l.cnpj }}</div>
            </td>
            <td><span class="cnae-badge">{{ l.cnae_principal }}</span><br>{{ l.cnae_descricao }}</td>
            <td><span class="pill {{ 'pill-sim' if l.optante_simples == 'Sim' else 'pill-nao' }}">{{ l.optante_simples or '—' }}</span></td>
            <td><span class="pill {{ 'pill-sim' if l.optante_mei == 'Sim' else 'pill-nao' }}">{{ l.optante_mei or '—' }}</span></td>
            <td>
              {% if l.telefone1 %}<a class="contato" href="tel:{{ l.ddd1 }}{{ l.telefone1 }}">({{ l.ddd1 }}) {{ l.telefone1 }}</a><br>{% endif %}
              {% if l.email %}<a class="contato" href="mailto:{{ l.email }}">{{ l.email }}</a>{% endif %}
            </td>
            <td>{{ l.endereco }}<br><span class="fantasia">{{ l.bairro }}</span></td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
    {% endif %}
  {% endif %}

</div>
</body>
</html>
"""

NOVIDADES_PAGINA = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>Empresas novas — Leads CNPJ</title>
""" + ESTILO + """
</head>
<body>
<div class="container">

  <div class="topo">
    <div>
      <h1>Empresas novas por município</h1>
      <p>Compara com a última vez que você conferiu esse município e mostra só o que abriu desde então.</p>
    </div>
    <a class="nav-link" href="/">← Voltar pra busca</a>
  </div>

  <div class="card">
    <form method="get" action="/novidades">
      <div class="linha-form">
        <div class="campo">
          <label>Cidade</label>
          <input type="text" name="cidade" value="{{ cidade or '' }}" placeholder="Ex.: São Paulo" required>
        </div>
        <div class="campo pequeno">
          <label>UF</label>
          <input type="text" name="uf" value="{{ uf or '' }}" maxlength="2" placeholder="SP">
        </div>
        <div class="campo">
          <label>Bairro (opcional)</label>
          <input type="text" name="bairro" value="{{ bairro or '' }}" placeholder="Município todo se em branco">
        </div>
        <div class="campo">
          <label>CNAE ou ramo (opcional)</label>
          <input type="text" name="cnae" value="{{ cnae or '' }}" placeholder="Ex.: contabilidade">
        </div>
      </div>
      <div class="checks">
        <label class="toggle"><input type="checkbox" name="apenas_simples" value="1" {% if apenas_simples %}checked{% endif %}> Só Simples Nacional</label>
        <label class="toggle"><input type="checkbox" name="apenas_mei" value="1" {% if apenas_mei %}checked{% endif %}> Só MEI</label>
      </div>
      <button type="submit" class="primario">Ver novidades</button>
    </form>
  </div>

  {% if erro_busca %}
    <div class="card"><div class="alerta alerta-erro">{{ erro_busca }}</div></div>
  {% endif %}

  {% if novas is not none %}
    <div class="card">
      {% if primeira_vez %}
        <div class="alerta alerta-info">
          Primeira checagem desse município (com esses filtros). Salvei as {{ total_atual }} empresa(s) atuais como ponto de partida —
          da próxima vez que você atualizar a base e checar de novo, só vai aparecer aqui quem abriu depois de hoje.
        </div>
      {% else %}
        <div class="resumo">
          <div>
            <div class="num">{{ novas|length }}</div>
            <div class="label">empresa(s) nova(s) desde a última checagem ({{ total_atual }} no total agora)</div>
          </div>
          {% if novas %}
            <a class="btn-csv" href="/novidades/csv?cidade={{ cidade }}&uf={{ uf }}&bairro={{ bairro or '' }}&cnae={{ cnae or '' }}&apenas_simples={{ 1 if apenas_simples else '' }}&apenas_mei={{ 1 if apenas_mei else '' }}">Baixar CSV</a>
          {% endif %}
        </div>
        {% if novas %}
        <form method="get" action="/novidades/confirmar" style="margin-top:10px">
          <input type="hidden" name="cidade" value="{{ cidade or '' }}">
          <input type="hidden" name="uf" value="{{ uf or '' }}">
          <input type="hidden" name="bairro" value="{{ bairro or '' }}">
          <input type="hidden" name="cnae" value="{{ cnae or '' }}">
          <input type="hidden" name="apenas_simples" value="{{ 1 if apenas_simples else '' }}">
          <input type="hidden" name="apenas_mei" value="{{ 1 if apenas_mei else '' }}">
          <button type="submit" class="secundario">Marcar essas como vistas</button>
        </form>
        <div class="alerta alerta-aviso" style="margin-top:10px">
          Enquanto você não clicar em "marcar como vistas", recarregar essa página sempre mostra essas mesmas novidades
          (não perde a lista se você atualizar por engano).
        </div>
        {% endif %}
      {% endif %}
    </div>

    {% if novas %}
    <div class="card" style="padding:0; overflow-x:auto">
      <table>
        <thead>
          <tr><th>Empresa</th><th>CNAE</th><th>Simples</th><th>MEI</th><th>Contato</th><th>Endereço</th><th>Aberta em</th></tr>
        </thead>
        <tbody>
        {% for l in novas[:max_linhas] %}
          <tr>
            <td>
              <div class="razao">{{ l.razao_social }}</div>
              {% if l.nome_fantasia %}<div class="fantasia">{{ l.nome_fantasia }}</div>{% endif %}
              <div class="fantasia">{{ l.cnpj }}</div>
            </td>
            <td><span class="cnae-badge">{{ l.cnae_principal }}</span><br>{{ l.cnae_descricao }}</td>
            <td><span class="pill {{ 'pill-sim' if l.optante_simples == 'Sim' else 'pill-nao' }}">{{ l.optante_simples or '—' }}</span></td>
            <td><span class="pill {{ 'pill-sim' if l.optante_mei == 'Sim' else 'pill-nao' }}">{{ l.optante_mei or '—' }}</span></td>
            <td>
              {% if l.telefone1 %}<a class="contato" href="tel:{{ l.ddd1 }}{{ l.telefone1 }}">({{ l.ddd1 }}) {{ l.telefone1 }}</a><br>{% endif %}
              {% if l.email %}<a class="contato" href="mailto:{{ l.email }}">{{ l.email }}</a>{% endif %}
            </td>
            <td>{{ l.endereco }}<br><span class="fantasia">{{ l.bairro }}</span></td>
            <td>{{ l.data_inicio_atividade }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
    {% endif %}
  {% endif %}

</div>
</body>
</html>
"""


def base_pronta():
    return os.path.exists(os.path.join(config.CACHE_DIR, "Municipios.zip"))


def baixar_em_background():
    status["baixando"] = True
    status["mensagem"] = "Baixando arquivos da Receita, isso pode levar 30-60 minutos..."
    status["erro"] = None
    try:
        base, zips = ba.escolher_base(None)
        alvo = [z for z in zips if ba.relevante(z)]
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        for nome in sorted(alvo):
            url = urljoin(base, nome)
            destino = os.path.join(config.CACHE_DIR, os.path.basename(nome))
            ba.baixar(url, destino, forcar=True)
        status["mensagem"] = "Base atualizada com sucesso. Pode buscar."
    except Exception as e:
        status["erro"] = str(e)
        status["mensagem"] = f"Deu erro ao baixar: {e}"
    finally:
        status["baixando"] = False


@app.route("/baixar")
def rota_baixar():
    if not status["baixando"]:
        threading.Thread(target=baixar_em_background, daemon=True).start()
    return redirect("/")


def _ler_params():
    cidade = request.args.get("cidade")
    uf = request.args.get("uf")
    bairro = request.args.get("bairro") or None
    cnae = request.args.get("cnae") or None
    apenas_simples = request.args.get("apenas_simples") in ("1", "true", "on")
    apenas_mei = request.args.get("apenas_mei") in ("1", "true", "on")
    return cidade, uf, bairro, cnae, apenas_simples, apenas_mei


@app.route("/")
def rota_index():
    cidade, uf, bairro, cnae, apenas_simples, apenas_mei = _ler_params()
    linhas = None
    erro_busca = None

    if cidade:
        if not base_pronta():
            erro_busca = 'Ainda nao tem base baixada. Clique em "Atualizar base da Receita" primeiro.'
        else:
            try:
                linhas = bl.buscar(
                    cache_dir=config.CACHE_DIR, cidade=cidade, uf=uf, bairro=bairro,
                    apenas_ativas=True, cnae=cnae, apenas_simples=apenas_simples, apenas_mei=apenas_mei,
                )
            except SystemExit as e:
                erro_busca = str(e)
            except Exception as e:
                traceback.print_exc()
                erro_busca = f"Deu um erro inesperado na busca: {e}\n(o detalhe completo apareceu no Terminal)"

    return render_template_string(
        PAGINA,
        cidade=cidade, uf=uf, bairro=bairro, cnae=cnae,
        apenas_simples=apenas_simples, apenas_mei=apenas_mei,
        linhas=linhas, erro_busca=erro_busca, max_linhas=MAX_LINHAS_TABELA,
        status_baixando=status["baixando"], status_msg=status["mensagem"], status_erro=status["erro"],
    )


@app.route("/csv")
def rota_csv():
    cidade, uf, bairro, cnae, apenas_simples, apenas_mei = _ler_params()
    try:
        linhas = bl.buscar(
            cache_dir=config.CACHE_DIR, cidade=cidade, uf=uf, bairro=bairro,
            apenas_ativas=True, cnae=cnae, apenas_simples=apenas_simples, apenas_mei=apenas_mei,
        )
    except SystemExit as e:
        return Response(str(e), status=400, mimetype="text/plain")
    except Exception as e:
        traceback.print_exc()
        return Response(f"Erro inesperado: {e}", status=500, mimetype="text/plain")

    buffer = io.StringIO()
    escritor = csv.DictWriter(buffer, fieldnames=bl.COLUNAS_SAIDA, extrasaction="ignore", restval="")
    escritor.writeheader()
    for l in linhas:
        escritor.writerow(l)

    nome_arquivo = f"leads_{cidade}_{bairro or 'municipio'}.csv".replace(" ", "_")
    return Response(
        "﻿" + buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )


def _calcular_novidades(cidade, uf, bairro, cnae, apenas_simples, apenas_mei):
    """Roda a busca atual e compara com o historico salvo (sem gravar nada
    ainda). Retorna (linhas_atuais, novas, vistos, ident, caminho_historico)."""
    os.makedirs(HISTORICO_WEB_DIR, exist_ok=True)
    linhas_atuais = bl.buscar(
        cache_dir=config.CACHE_DIR, cidade=cidade, uf=uf, bairro=bairro,
        apenas_ativas=True, cnae=cnae, apenas_simples=apenas_simples, apenas_mei=apenas_mei,
    )
    ident = mn.identificador_busca(cidade, uf, bairro, cnae)
    caminho_historico = os.path.join(HISTORICO_WEB_DIR, f"historico_{ident}.json")
    vistos = mn.carregar_historico(caminho_historico)
    atuais_basicos = {mn.basico_de_cnpj(l["cnpj"]) for l in linhas_atuais}
    novos_basicos = atuais_basicos - vistos
    novas = [l for l in linhas_atuais if mn.basico_de_cnpj(l["cnpj"]) in novos_basicos]
    return linhas_atuais, novas, vistos, atuais_basicos, caminho_historico


@app.route("/novidades")
def rota_novidades():
    cidade, uf, bairro, cnae, apenas_simples, apenas_mei = _ler_params()
    novas = None
    total_atual = 0
    primeira_vez = False
    erro_busca = None

    if cidade:
        if not base_pronta():
            erro_busca = 'Ainda nao tem base baixada. Clique em "Atualizar base da Receita" primeiro (na tela de busca).'
        else:
            try:
                linhas_atuais, novas, vistos, atuais_basicos, caminho_historico = _calcular_novidades(
                    cidade, uf, bairro, cnae, apenas_simples, apenas_mei
                )
                total_atual = len(linhas_atuais)
                primeira_vez = len(vistos) == 0
                if primeira_vez:
                    # primeira checagem desse municipio/filtro: nao ha "novidade"
                    # pra mostrar ainda, entao ja salva o ponto de partida
                    # automaticamente pra proxima checagem ter o que comparar.
                    mn.salvar_historico(caminho_historico, atuais_basicos)
                    novas = []
            except SystemExit as e:
                erro_busca = str(e)
            except Exception as e:
                traceback.print_exc()
                erro_busca = f"Deu um erro inesperado: {e}\n(o detalhe completo apareceu no Terminal)"

    return render_template_string(
        NOVIDADES_PAGINA,
        cidade=cidade, uf=uf, bairro=bairro, cnae=cnae,
        apenas_simples=apenas_simples, apenas_mei=apenas_mei,
        novas=novas, total_atual=total_atual, primeira_vez=primeira_vez,
        erro_busca=erro_busca, max_linhas=MAX_LINHAS_TABELA,
    )


@app.route("/novidades/csv")
def rota_novidades_csv():
    cidade, uf, bairro, cnae, apenas_simples, apenas_mei = _ler_params()
    try:
        _, novas, _, _, _ = _calcular_novidades(cidade, uf, bairro, cnae, apenas_simples, apenas_mei)
    except SystemExit as e:
        return Response(str(e), status=400, mimetype="text/plain")
    except Exception as e:
        traceback.print_exc()
        return Response(f"Erro inesperado: {e}", status=500, mimetype="text/plain")

    buffer = io.StringIO()
    escritor = csv.DictWriter(buffer, fieldnames=bl.COLUNAS_SAIDA, extrasaction="ignore", restval="")
    escritor.writeheader()
    for l in novas:
        escritor.writerow(l)

    nome_arquivo = f"novidades_{cidade}_{bairro or 'municipio'}.csv".replace(" ", "_")
    return Response(
        "﻿" + buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )


@app.route("/novidades/confirmar")
def rota_novidades_confirmar():
    cidade, uf, bairro, cnae, apenas_simples, apenas_mei = _ler_params()
    if cidade and base_pronta():
        try:
            _, _, vistos, atuais_basicos, caminho_historico = _calcular_novidades(
                cidade, uf, bairro, cnae, apenas_simples, apenas_mei
            )
            mn.salvar_historico(caminho_historico, vistos | atuais_basicos)
        except Exception:
            traceback.print_exc()
    return redirect(
        f"/novidades?cidade={cidade or ''}&uf={uf or ''}&bairro={bairro or ''}&cnae={cnae or ''}"
        f"&apenas_simples={'1' if apenas_simples else ''}&apenas_mei={'1' if apenas_mei else ''}"
    )


if __name__ == "__main__":
    def abrir_navegador():
        time.sleep(1.2)
        webbrowser.open("http://127.0.0.1:5000")

    threading.Thread(target=abrir_navegador, daemon=True).start()
    app.run(port=5000, debug=False)
