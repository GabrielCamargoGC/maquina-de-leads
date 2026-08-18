#!/usr/bin/env python3
r"""
Metadados da base instalada: qual mes da Receita esta no ar, quando foi
trocada, quantas linhas tem.

Fica num JSON ao lado dos dados em vez de ser deduzido na hora porque contar
72 M de linhas para desenhar o rodape do site seria absurdo -- o job de
atualizacao ja sabe esses numeros e os anota de graca.
"""
import json
from datetime import datetime
from pathlib import Path

from . import config

NOME = "base.json"


def caminho(dir_dados=None):
    return Path(dir_dados or config.DIR_ATUAL) / NOME


def ler(dir_dados=None):
    p = caminho(dir_dados)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def gravar(dir_dados, referencia=None, linhas=None, municipios=None, ufs=None):
    p = caminho(dir_dados)
    p.parent.mkdir(parents=True, exist_ok=True)
    dados = {
        "referencia": referencia,
        "linhas": linhas,
        "municipios": municipios,
        "ufs": ufs,
        "atualizada_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }
    p.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    return dados
