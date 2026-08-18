#!/usr/bin/env python3
r"""
Descoberta e download dos arquivos de Dados Abertos do CNPJ.

Herdado do baixar_arquivos.py original, com duas adicoes que o job diario
precisa:

  referencia_publicada()  diz qual mes (AAAA-MM) esta publicado agora, sem
                          baixar nada. E o que permite rodar todos os dias
                          gastando ~50 KB nos 29 dias em que nada mudou.

  baixar_todos()          baixa com retomada: arquivo ja completo e pulado,
                          arquivo parcial (.part) continua de onde parou.
                          Importa porque sao 7,3 GB e uma queda de conexao
                          no meio nao pode obrigar a comecar do zero.

Desde fev/2026 a Receita publica num compartilhamento Nextcloud, e nao mais
numa pasta HTML. Existem varias formas de acessar um compartilhamento publico
de Nextcloud via WebDAV -- o codigo tenta todas em sequencia, porque nao da
para saber de fora qual delas essa instalacao habilitou.
"""
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests

from . import config, layout

TIMEOUT = 60
CHUNK = 4 * 1024 * 1024
TENTATIVAS = 4

PROPFIND = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
            b'<d:prop><d:resourcetype/></d:prop></d:propfind>')

VARIANTES_WEBDAV = [
    "/public.php/webdav/",
    "/remote.php/dav/public-files/{token}/",
    "/public.php/dav/files/{token}/",
]


class ErroFonte(Exception):
    pass


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _propfind(url, auth):
    return requests.request(
        "PROPFIND", url,
        headers={"Depth": "1", "Content-Type": "text/xml"},
        data=PROPFIND, auth=auth, timeout=TIMEOUT,
    )


def _parse(conteudo):
    ns = {"d": "DAV:"}
    itens = []
    for i, r in enumerate(ET.fromstring(conteudo).findall("d:response", ns)):
        if i == 0:
            continue  # o primeiro e a propria pasta consultada
        href = r.find("d:href", ns)
        if href is None or href.text is None:
            continue
        nome = unquote(href.text).rstrip("/").split("/")[-1]
        e_pasta = r.find(".//d:resourcetype/d:collection", ns) is not None
        itens.append((nome, e_pasta))
    return itens


def _raiz_webdav():
    erros = []
    for template in VARIANTES_WEBDAV:
        url = f"{config.NEXTCLOUD_HOST}{template.format(token=config.NEXTCLOUD_TOKEN)}"
        for auth in [(config.NEXTCLOUD_TOKEN, ""), None]:
            try:
                resp = _propfind(url, auth)
                if resp.status_code == 207:
                    return url, auth, _parse(resp.content)
                erros.append(f"{url}: HTTP {resp.status_code}")
            except Exception as e:
                erros.append(f"{url}: {e}")
    raise ErroFonte("nenhuma variante de WebDAV respondeu. " + " ; ".join(erros[:6]))


def _zips_de(itens):
    return [n for n, pasta in itens if not pasta and n.lower().endswith(".zip")]


def _pastas_mes(itens):
    return sorted({n for n, pasta in itens if pasta and re.fullmatch(r"\d{4}-\d{2}", n)},
                  reverse=True)


def localizar():
    """Devolve (url_base, auth, lista_de_zips, referencia).

    referencia e o 'AAAA-MM' da pasta usada, ou None quando os zips estao
    soltos na raiz (a Receita ja publicou dos dois jeitos).
    """
    raiz, auth, itens = _raiz_webdav()

    zips = _zips_de(itens)
    if zips:
        return raiz, auth, zips, None

    for pasta in _pastas_mes(itens):
        resp = _propfind(f"{raiz}{pasta}/", auth)
        resp.raise_for_status()
        sub = _parse(resp.content)
        sub_zips = _zips_de(sub)
        if sub_zips:
            return f"{raiz}{pasta}/", auth, sub_zips, pasta

    raise ErroFonte("achei o Nextcloud da Receita, mas nenhuma pasta tem .zip dentro")


def referencia_publicada():
    """So a referencia (AAAA-MM), sem listar nem baixar arquivo.

    E a pergunta que o job faz todos os dias. Custa uma requisicao.
    """
    raiz, auth, itens = _raiz_webdav()
    pastas = _pastas_mes(itens)
    if pastas:
        return pastas[0]
    return "raiz" if _zips_de(itens) else None


def relevante(nome):
    alvos = layout.PREFIXOS_MULTIPARTE + layout.ARQUIVOS_UNICOS
    return any(nome.lower().startswith(p.lower()) for p in alvos)


def _baixar_um(url, destino, auth):
    """Download com retomada por Range. Um .part de 2 GB nao pode ser jogado
    fora porque a conexao caiu no fim."""
    destino = Path(destino)
    parcial = destino.with_suffix(destino.suffix + ".part")

    for tentativa in range(1, TENTATIVAS + 1):
        ja_tem = parcial.stat().st_size if parcial.exists() else 0
        headers = {"User-Agent": "Mozilla/5.0"}
        if ja_tem:
            headers["Range"] = f"bytes={ja_tem}-"
        try:
            with requests.get(url, stream=True, timeout=TIMEOUT,
                              headers=headers, auth=auth) as r:
                if ja_tem and r.status_code == 200:
                    ja_tem = 0  # servidor ignorou o Range: recomeca
                elif r.status_code not in (200, 206):
                    r.raise_for_status()

                total = int(r.headers.get("content-length", 0)) + ja_tem
                modo = "ab" if ja_tem else "wb"
                lido = ja_tem
                marco = lido
                with open(parcial, modo) as f:
                    for pedaco in r.iter_content(chunk_size=CHUNK):
                        f.write(pedaco)
                        lido += len(pedaco)
                        if total and lido - marco >= 200 * 1024 * 1024:
                            marco = lido
                            _log(f"    {destino.name}: {lido*100//total}% "
                                 f"({lido/1e9:.1f}/{total/1e9:.1f} GB)")
            os.replace(parcial, destino)
            return destino.stat().st_size
        except Exception as e:
            if tentativa == TENTATIVAS:
                raise
            espera = 5 * tentativa
            _log(f"    {destino.name}: falhou ({e}); tentativa "
                 f"{tentativa+1}/{TENTATIVAS} em {espera}s")
            time.sleep(espera)


def baixar_todos(dir_destino, forcar=False):
    """Baixa os zips que interessam. Devolve (referencia, quantidade, bytes)."""
    base, auth, zips, referencia = localizar()
    alvo = sorted(z for z in zips if relevante(z))
    if not alvo:
        raise ErroFonte("a pasta encontrada nao tem Estabelecimentos/Empresas/"
                        "Municipios/Cnaes/Simples")

    dir_destino = Path(dir_destino)
    dir_destino.mkdir(parents=True, exist_ok=True)
    _log(f"Fonte: {base} ({len(alvo)} arquivos, referencia={referencia or 'raiz'})")

    total_bytes = 0
    for nome in alvo:
        destino = dir_destino / os.path.basename(nome)
        if not forcar and destino.exists() and destino.stat().st_size > 0:
            _log(f"    {destino.name}: ja existe, pulando")
            total_bytes += destino.stat().st_size
            continue
        _log(f"    {destino.name}: baixando")
        total_bytes += _baixar_um(urljoin(base, nome), destino, auth)

    return referencia, len(alvo), total_bytes
