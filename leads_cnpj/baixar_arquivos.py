#!/usr/bin/env python3
"""
Baixa os arquivos de Dados Abertos do CNPJ (Receita Federal) necessarios
para buscar_leads.py: Estabelecimentos*.zip, Empresas*.zip, Municipios.zip,
Cnaes.zip, Simples.zip.

Desde fev/2026 a Receita disponibiliza esses arquivos por um
compartilhamento publico de Nextcloud (nao mais uma pasta HTML comum).
Existem varias formas conhecidas de acessar um compartilhamento publico de
Nextcloud via WebDAV -- este script tenta todas elas em sequencia ate uma
funcionar, porque nao da pra saber de antemao qual essa instalacao especifica
da Receita habilitou.
"""
import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urljoin

import requests

import config

TIMEOUT = 30
CHUNK = 1024 * 1024  # 1MB

PROPFIND_BODY = b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'

_VARIANTES_WEBDAV = [
    "/public.php/webdav/",
    "/remote.php/dav/public-files/{token}/",
    "/public.php/dav/files/{token}/",
]

_estado = {"auth": None}


def _parse_propfind(conteudo_xml):
    ns = {"d": "DAV:"}
    root = ET.fromstring(conteudo_xml)
    respostas = root.findall("d:response", ns)
    itens = []
    for i, r in enumerate(respostas):
        href_el = r.find("d:href", ns)
        if href_el is None or href_el.text is None:
            continue
        href = unquote(href_el.text)
        e_pasta = r.find(".//d:resourcetype/d:collection", ns) is not None
        nome = href.rstrip("/").split("/")[-1]
        if i == 0:
            continue
        itens.append((nome, e_pasta))
    return itens


def _tentar_propfind(url, auth):
    return requests.request(
        "PROPFIND",
        url,
        headers={"Depth": "1", "Content-Type": "text/xml"},
        data=PROPFIND_BODY,
        auth=auth,
        timeout=TIMEOUT,
    )


def _achar_webdav_raiz():
    erros = []
    for template in _VARIANTES_WEBDAV:
        caminho = template.format(token=config.NEXTCLOUD_TOKEN)
        url = f"{config.NEXTCLOUD_HOST}{caminho}"
        for auth in [(config.NEXTCLOUD_TOKEN, ""), None]:
            try:
                resp = _tentar_propfind(url, auth)
                if resp.status_code == 207:
                    return url, auth, _parse_propfind(resp.content)
                erros.append(f"{url} (auth={'token' if auth else 'nenhum'}): HTTP {resp.status_code}")
            except Exception as e:
                erros.append(f"{url} (auth={'token' if auth else 'nenhum'}): {e}")
    raise RuntimeError("nenhuma variante de WebDAV respondeu. Detalhes: " + " ; ".join(erros))


def _achar_pasta_com_zips():
    raiz, auth, itens = _achar_webdav_raiz()
    zips = [n for n, pasta in itens if not pasta and n.lower().endswith(".zip")]
    if zips:
        return raiz, auth, itens

    pastas_data = sorted(
        {n for n, pasta in itens if pasta and re.fullmatch(r"\d{4}-\d{2}", n)}, reverse=True
    )
    for pasta in pastas_data:
        sub_url = f"{raiz}{pasta}/"
        resp = _tentar_propfind(sub_url, auth)
        resp.raise_for_status()
        sub_itens = _parse_propfind(resp.content)
        sub_zips = [n for n, p in sub_itens if not p and n.lower().endswith(".zip")]
        if sub_zips:
            return sub_url, auth, sub_itens

    raise RuntimeError("achei o Nextcloud da Receita mas nenhuma pasta dentro dele tem arquivo .zip")


def _listar_links_legado(url):
    resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return re.findall(r'href="([^"?][^"]*)"', resp.text)


def _pasta_mais_recente_legado(base_url):
    links = _listar_links_legado(base_url)
    pastas_data = sorted({l for l in links if re.fullmatch(r"\d{4}-\d{2}/?", l)}, reverse=True)
    if pastas_data:
        return urljoin(base_url, pastas_data[0])
    return base_url


def _escolher_base_legado(base_url_manual):
    pasta = _pasta_mais_recente_legado(base_url_manual)
    links = _listar_links_legado(pasta)
    zips = [l for l in links if l.lower().endswith(".zip")]
    if not zips:
        raise RuntimeError(f"nao achei .zip em {pasta}")
    return pasta, zips


def escolher_base(base_url_manual):
    if base_url_manual:
        return _escolher_base_legado(base_url_manual)

    try:
        raiz, auth, itens = _achar_pasta_com_zips()
        zips = [n for n, pasta in itens if not pasta and n.lower().endswith(".zip")]
        _estado["auth"] = auth
        print(f"[ok] usando Nextcloud da Receita: {raiz} ({len(zips)} arquivos .zip, auth={'token' if auth else 'nenhum'})")
        return raiz, zips
    except Exception as e:
        print(f"[aviso] Nextcloud da Receita falhou: {e}")

    for base in config.CANDIDATE_BASES:
        try:
            pasta = _pasta_mais_recente_legado(base)
            links = _listar_links_legado(pasta)
            zips = [l for l in links if l.lower().endswith(".zip")]
            if zips:
                print(f"[ok] usando base (modo antigo): {pasta} ({len(zips)} arquivos .zip)")
                _estado["auth"] = None
                return pasta, zips
        except Exception as e:
            print(f"[aviso] base {base} falhou: {e}")

    raise RuntimeError(
        "Nao consegui encontrar automaticamente os arquivos .zip (nem via "
        "Nextcloud, nem no modo antigo). Confira "
        "https://www.gov.br/receitafederal/dados e rode de novo com "
        "--base-url apontando para a pasta correta."
    )


def relevante(nome_arquivo):
    for prefixo in config.PREFIXOS_MULTIPARTE + config.ARQUIVOS_UNICOS:
        if nome_arquivo.lower().startswith(prefixo.lower()):
            return True
    return False


def baixar(url, destino, forcar=False):
    if not forcar and os.path.exists(destino) and os.path.getsize(destino) > 0:
        print(f"  ja existe, pulando: {os.path.basename(destino)}")
        return
    print(f"  baixando: {os.path.basename(destino)}")
    auth = _estado["auth"] if config.NEXTCLOUD_HOST in url else None
    with requests.get(url, stream=True, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}, auth=auth) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        lido = 0
        tmp = destino + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK):
                f.write(chunk)
                lido += len(chunk)
                if total:
                    pct = lido * 100 // total
                    print(f"\r    {pct}% ({lido/1e6:.0f}MB/{total/1e6:.0f}MB)", end="")
        print()
        os.replace(tmp, destino)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", help="URL manual (modo antigo, pasta HTML comum), caso o Nextcloud da Receita falhe")
    ap.add_argument("--cache-dir", default=config.CACHE_DIR)
    ap.add_argument("--forcar", action="store_true", help="rebaixa mesmo se o arquivo ja existir no cache (use para pegar a atualizacao mensal)")
    args = ap.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)

    base, zips = escolher_base(args.base_url)

    alvo = [z for z in zips if relevante(z)]
    if not alvo:
        print("[erro] a pasta encontrada nao tem arquivos Estabelecimentos/Empresas/Municipios/Cnaes/Simples.")
        sys.exit(1)

    print(f"Baixando {len(alvo)} arquivo(s) para {args.cache_dir}/ ...")
    for nome in sorted(alvo):
        url = urljoin(base, nome)
        destino = os.path.join(args.cache_dir, os.path.basename(nome))
        try:
            baixar(url, destino, forcar=args.forcar)
        except Exception as e:
            print(f"  [erro] falhou {nome}: {e}")

    print("Concluido. Rode buscar_leads.py para consultar (ou use o app.py).")


if __name__ == "__main__":
    main()
