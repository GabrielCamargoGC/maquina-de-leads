#!/usr/bin/env python3
r"""
Saude da maquina: memoria, disco, CPU, tempo ligado e servicos.

Feito sem psutil de proposito. A promessa do projeto e nao ter dependencia
alem do que o Python ja traz, e tudo aqui sai de ctypes no Windows e de
/proc no Linux -- o mesmo que a biblioteca faria por baixo.

A CPU e o unico numero que exige medir duas vezes: uso e trabalho dividido
por tempo decorrido, e uma leitura solta nao tem "decorrido". Em vez de
dormir um segundo (o que travaria a pagina do master), guarda a leitura
anterior e compara com ela. A primeira visita depois de subir o servico
mostra "—"; da segunda em diante, mostra o uso desde a visita passada.
"""
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, estado

_cpu_anterior = {"ocupado": None, "total": None}


def _pct(parte, total):
    if not total:
        return 0.0
    return round(parte / total * 100, 1)


def _fmt_bytes(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_duracao(segundos):
    if segundos is None:
        return "—"
    d = timedelta(seconds=int(segundos))
    dias, resto = d.days, d.seconds
    horas, minutos = resto // 3600, (resto % 3600) // 60
    if dias:
        return f"{dias}d {horas}h"
    if horas:
        return f"{horas}h {minutos}min"
    return f"{minutos}min"


# ------------------------------------------------------------ memoria


def memoria():
    """(usado_bytes, total_bytes) da maquina inteira."""
    if os.name == "nt":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        s = Status()
        s.dwLength = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
            return None, None
        return s.ullTotalPhys - s.ullAvailPhys, s.ullTotalPhys

    try:
        campos = {}
        for linha in Path("/proc/meminfo").read_text().splitlines():
            nome, _, valor = linha.partition(":")
            campos[nome.strip()] = int(valor.split()[0]) * 1024
        total = campos.get("MemTotal", 0)
        # MemAvailable, e nao MemFree: cache conta como disponivel, e usar
        # MemFree faria a maquina parecer sempre sem memoria.
        livre = campos.get("MemAvailable", campos.get("MemFree", 0))
        return total - livre, total
    except (OSError, ValueError, KeyError):
        return None, None


# ------------------------------------------------------------ cpu


def _amostra_cpu():
    """(ocupado, total) em unidades arbitrarias, para comparar com a anterior."""
    if os.name == "nt":
        import ctypes

        class Tempo(ctypes.Structure):
            _fields_ = [("baixo", ctypes.c_ulong), ("alto", ctypes.c_ulong)]

        ocioso, kernel, usuario = Tempo(), Tempo(), Tempo()
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(ocioso), ctypes.byref(kernel), ctypes.byref(usuario)
        )
        if not ok:
            return None, None
        val = lambda t: (t.alto << 32) | t.baixo
        # No Windows o tempo de kernel JA inclui o ocioso -- somar os tres
        # daria mais de 100% de uso.
        total = val(kernel) + val(usuario)
        return total - val(ocioso), total

    try:
        partes = Path("/proc/stat").read_text().split("\n")[0].split()[1:]
        numeros = [int(x) for x in partes]
        total = sum(numeros)
        ocioso = numeros[3] + (numeros[4] if len(numeros) > 4 else 0)
        return total - ocioso, total
    except (OSError, ValueError, IndexError):
        return None, None


def cpu():
    """Uso desde a leitura anterior, em %. None na primeira chamada."""
    ocupado, total = _amostra_cpu()
    if ocupado is None:
        return None
    antes_o, antes_t = _cpu_anterior["ocupado"], _cpu_anterior["total"]
    _cpu_anterior["ocupado"], _cpu_anterior["total"] = ocupado, total
    if antes_o is None or total == antes_t:
        return None
    return max(0.0, min(100.0, _pct(ocupado - antes_o, total - antes_t)))


# ------------------------------------------------------------ tempo ligado


def ligado_ha():
    if os.name == "nt":
        import ctypes

        return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------ servicos


def servico(nome):
    """'no ar', 'parado' ou 'nao instalado'."""
    try:
        if os.name == "nt":
            r = subprocess.run(["sc.exe", "query", nome], capture_output=True,
                               text=True, timeout=10)
            if r.returncode != 0:
                return "nao instalado"
            return "no ar" if "RUNNING" in r.stdout.upper() else "parado"
        r = subprocess.run(["systemctl", "is-active", nome], capture_output=True,
                           text=True, timeout=10)
        if r.returncode not in (0, 3):
            return "nao instalado"
        return "no ar" if r.stdout.strip() == "active" else "parado"
    except (OSError, subprocess.TimeoutExpired):
        return "nao instalado"


# ------------------------------------------------------------ tudo junto


def coletar():
    usada, total_ram = memoria()
    disco = shutil.disk_usage(config.DIR_DADOS if config.DIR_DADOS.exists()
                              else Path.cwd())
    info = estado.ler()
    nome_servico = os.environ.get("LEADS_SERVICO", "LeadsCNPJ")

    tamanho_base = 0
    if config.DIR_ATUAL.exists():
        tamanho_base = sum(f.stat().st_size
                           for f in config.DIR_ATUAL.rglob("*") if f.is_file())

    return {
        "ram_usada": usada,
        "ram_total": total_ram,
        "ram_pct": _pct(usada, total_ram) if usada is not None else None,
        "ram_texto": (f"{_fmt_bytes(usada)} de {_fmt_bytes(total_ram)}"
                      if usada is not None else "—"),

        "disco_usado": disco.used,
        "disco_total": disco.total,
        "disco_livre": disco.free,
        "disco_pct": _pct(disco.used, disco.total),
        "disco_texto": f"{_fmt_bytes(disco.free)} livres de {_fmt_bytes(disco.total)}",

        "cpu_pct": cpu(),
        "ligado_ha": _fmt_duracao(ligado_ha()),

        "servico_site": servico(nome_servico),
        "servico_tunel": servico(f"{nome_servico}-Tunel"),

        "base_referencia": info.get("referencia") or "—",
        "base_linhas": info.get("linhas"),
        "base_municipios": info.get("municipios"),
        "base_atualizada": info.get("atualizada_em") or "—",
        "base_tamanho": _fmt_bytes(tamanho_base) if tamanho_base else "—",
        "tem_anterior": (config.DIR_ANTERIOR / "empresas_final").exists(),

        "agora": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    }
