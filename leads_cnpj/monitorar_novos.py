#!/usr/bin/env python3
"""
Roda a busca de cidade (e opcionalmente bairro/CNAE/Simples/MEI) de novo,
atualiza a base da Receita, e mantem um historico para detectar empresas
NOVAS desde a ultima execucao.

Feito para ser chamado 1x por mes por uma tarefa agendada (cron / Agendador
de Tarefas do Windows) -- a Receita so libera dados novos nessa cadencia,
entao rodar com mais frequencia so vai reprocessar os mesmos dados.

Essas mesmas funcoes (identificador_busca, carregar_historico, etc.) tambem
sao reusadas pelo app.py na tela "Empresas novas por municipio".

Gera, na pasta de saida:
  - leads_atual_<busca>.csv       snapshot completo mais recente (sobrescrito)
  - novos_detectados_<busca>.csv  log cumulativo, so das empresas novas,
                                  com coluna "detectado_em" (uma linha por
                                  empresa nova, nunca apaga o que ja tinha)
  - historico_<busca>.json        controle interno (nao mexer)

Exemplo:
    python monitorar_novos.py --cidade "Sao Paulo" --uf SP --bairro "Pinheiros" \\
        --cnae contabilidade --pasta-saida ./monitoramento
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys
from urllib.parse import urljoin

import baixar_arquivos as ba
import buscar_leads as bl
import config


def identificador_busca(cidade, uf, bairro, cnae=None):
    bruto = f"{cidade}_{uf or ''}_{bairro or 'municipio'}_{cnae or ''}"
    return re.sub(r"[^a-zA-Z0-9]+", "_", bruto).strip("_").lower()


def basico_de_cnpj(cnpj_formatado):
    return re.sub(r"\D", "", cnpj_formatado)[:8]


def carregar_historico(caminho):
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def salvar_historico(caminho, cnpjs_basicos):
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(sorted(cnpjs_basicos), f, ensure_ascii=False)


def atualizar_base(cache_dir, base_url_manual):
    print("Atualizando base da Receita (baixando arquivos do mes)...")
    base, zips = ba.escolher_base(base_url_manual)
    alvo = [z for z in zips if ba.relevante(z)]
    if not alvo:
        sys.exit("[erro] nao encontrei os arquivos esperados na base da Receita.")
    os.makedirs(cache_dir, exist_ok=True)
    for nome in sorted(alvo):
        url = urljoin(base, nome)
        destino = os.path.join(cache_dir, os.path.basename(nome))
        try:
            ba.baixar(url, destino, forcar=True)
        except Exception as e:
            print(f"  [erro] falhou {nome}: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cidade", required=True)
    ap.add_argument("--bairro", default=None, help="opcional; sem isso monitora o municipio inteiro")
    ap.add_argument("--uf")
    ap.add_argument("--cnae", help='codigo (ou prefixo) de CNAE, ou palavra-chave, ex.: "contabilidade"')
    ap.add_argument("--apenas-simples", action="store_true", help="so empresas optantes pelo Simples Nacional")
    ap.add_argument("--apenas-mei", action="store_true", help="so MEI")
    ap.add_argument("--cache-dir", default=config.CACHE_DIR)
    ap.add_argument("--pasta-saida", default=".")
    ap.add_argument("--base-url", help="URL manual, se a deteccao automatica da Receita falhar")
    ap.add_argument("--pular-download", action="store_true", help="nao rebaixa, so reprocessa o cache que ja existe (use so para testar)")
    ap.add_argument("--todas-situacoes", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.pasta_saida, exist_ok=True)

    if not args.pular_download:
        atualizar_base(args.cache_dir, args.base_url)

    linhas = bl.buscar(
        cache_dir=args.cache_dir,
        cidade=args.cidade,
        uf=args.uf,
        bairro=args.bairro,
        apenas_ativas=not args.todas_situacoes,
        cnae=args.cnae,
        apenas_simples=args.apenas_simples,
        apenas_mei=args.apenas_mei,
    )

    ident = identificador_busca(args.cidade, args.uf, args.bairro, args.cnae)
    caminho_historico = os.path.join(args.pasta_saida, f"historico_{ident}.json")
    caminho_atual = os.path.join(args.pasta_saida, f"leads_atual_{ident}.csv")
    caminho_novos = os.path.join(args.pasta_saida, f"novos_detectados_{ident}.csv")

    vistos = carregar_historico(caminho_historico)
    atuais = {basico_de_cnpj(l["cnpj"]) for l in linhas}
    primeira_execucao = len(vistos) == 0
    novos_basicos = atuais - vistos

    bl.salvar_csv(linhas, caminho_atual)

    novos = [l for l in linhas if basico_de_cnpj(l["cnpj"]) in novos_basicos]
    if primeira_execucao:
        novos = []
        print("Primeira execucao: salvando o historico inicial (sem gerar novidades).")

    if novos:
        hoje = datetime.date.today().isoformat()
        existe = os.path.exists(caminho_novos)
        with open(caminho_novos, "a", newline="", encoding="utf-8-sig") as f:
            campos = ["detectado_em"] + bl.COLUNAS_SAIDA
            escritor = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore", restval="")
            if not existe:
                escritor.writeheader()
            for l in novos:
                escritor.writerow({"detectado_em": hoje, **l})

    salvar_historico(caminho_historico, vistos | atuais)

    alvo_txt = f"{args.cidade}/{args.bairro}" if args.bairro else f"{args.cidade} (municipio inteiro)"
    print(f"{len(linhas)} empresa(s) encontradas no total para {alvo_txt}.")
    print(f"Snapshot atualizado: {caminho_atual}")
    if novos:
        print(f"{len(novos)} empresa(s) NOVA(S) desde a ultima checagem -> {caminho_novos}")
    elif not primeira_execucao:
        print("Nenhuma empresa nova desde a ultima checagem.")


if __name__ == "__main__":
    main()
