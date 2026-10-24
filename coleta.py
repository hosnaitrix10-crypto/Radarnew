#!/usr/bin/env python3
"""Gera dados.json para o Radar de Dividendos usando Fundamentus e Yahoo Finance."""
from __future__ import annotations
import datetime as dt
import json
import math
import time
from io import StringIO
from pathlib import Path
import pandas as pd
import requests
import yfinance as yf

OUT = Path("dados.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RadarDeDividendos/1.0)"}
TIMEOUT = 60
PAUSA_SEGUNDOS = 0.5


def numero(valor):
    """Converte número ou texto pt-BR; dados ausentes viram None."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)) and not pd.isna(valor):
        return float(valor)
    texto = str(valor).strip().replace("%", "").replace("R$", "").replace(" ", "")
    if not texto or texto.lower() in {"nan", "none", "-", "--"}:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        resultado = float(texto)
        return resultado if math.isfinite(resultado) else None
    except (TypeError, ValueError):
        return None


def nome_coluna(df, prefixo):
    for coluna in df.columns:
        if str(coluna).startswith(prefixo):
            return coluna
    raise KeyError(f"Coluna não encontrada: {prefixo}")


def carregar_fundamentus():
    resposta = requests.get("https://www.fundamentus.com.br/resultado.php", headers=HEADERS, timeout=TIMEOUT)
    resposta.raise_for_status()
    tabelas = pd.read_html(StringIO(resposta.text), decimal=",", thousands=".")
    if not tabelas:
        raise RuntimeError("O Fundamentus não retornou nenhuma tabela.")
    df = tabelas[0]
    prefixos = {"t": "Papel", "p": "Cota", "pl": "P/L", "pvp": "P/VP", "dy": "Div", "ml": "Mrg. L", "roe": "ROE", "liq": "Liq.2", "dbp": "D", "cr": "Cresc"}
    col = {k: nome_coluna(df, v) for k, v in prefixos.items()}
    por_empresa = {}
    for _, linha in df.iterrows():
        ticker = str(linha[col["t"]]).strip().upper()
        if not ticker or ticker in {"NAN", "-"}:
            continue
        a = {k: numero(linha[c]) for k, c in col.items()}
        a["t"] = ticker
        # Mantém os filtros fundamentais do ranking original.
        if not (a["liq"] is not None and a["liq"] >= 1e6 and a["pl"] is not None and a["pl"] > 0 and a["pvp"] is not None and a["pvp"] > 0 and a["roe"] is not None and a["roe"] > 0 and a["p"] is not None and a["p"] > 0):
            continue
        chave = ticker[:4]
        anterior = por_empresa.get(chave)
        if anterior is None or (a["liq"] or 0) > (anterior["liq"] or 0):
            por_empresa[chave] = a
    if not por_empresa:
        raise RuntimeError("Nenhum ativo passou pelos filtros; o dados.json anterior será preservado.")
    return list(por_empresa.values())


def data_sem_fuso(indice):
    idx = pd.DatetimeIndex(indice)
    return idx.tz_localize(None) if idx.tz is not None else idx


def enriquecer_yahoo(ativo, agora):
    try:
        acao = yf.Ticker(ativo["t"] + ".SA")
        info = acao.info or {}
        historico = acao.history(period="1y", auto_adjust=False)
        dividendos = acao.dividends
        if dividendos is None:
            dividendos = pd.Series(dtype=float)
        if len(dividendos):
            dividendos.index = data_sem_fuso(dividendos.index)
        lows = historico["Low"].dropna() if not historico.empty and "Low" in historico else pd.Series(dtype=float)
        highs = historico["High"].dropna() if not historico.empty and "High" in historico else pd.Series(dtype=float)
        ativo["nome"] = info.get("shortName") or info.get("longName") or ativo["t"]
        ativo["setor"] = info.get("sector") or ""
        ativo["lo"] = float(lows.min()) if not lows.empty else None
        ativo["hi"] = float(highs.max()) if not highs.empty else None
        if len(dividendos):
            div_5a = dividendos[dividendos.index >= agora - pd.DateOffset(years=5)]
            anos = len(set(int(a) for a in div_5a.index.year))
            ativo["ap"] = min(5, anos)
            ativo["dpa"] = round(float(div_5a.sum()) / anos, 4) if anos else None
            div_recent = dividendos[dividendos.index >= agora - pd.DateOffset(months=18)]
            ativo["div"] = [{"v": float(v), "d": d.strftime("%Y-%m-%d")} for d, v in div_recent.items() if math.isfinite(float(v))]
        else:
            ativo["ap"], ativo["dpa"], ativo["div"] = 0, None, []
        ativo["dados_yahoo"] = "ok"
    except Exception as erro:
        # Não descarta os fundamentos caso o provedor secundário falhe.
        ativo.setdefault("nome", ativo["t"])
        ativo.setdefault("setor", "")
        ativo.setdefault("lo", None)
        ativo.setdefault("hi", None)
        ativo.setdefault("dpa", None)
        ativo.setdefault("ap", 0)
        ativo.setdefault("div", [])
        ativo["dados_yahoo"] = "indisponivel"
        ativo["erro_yahoo"] = str(erro)[:240]
        print(f"[AVISO] {ativo['t']}: Yahoo indisponível: {erro}")
    return ativo


def main():
    agora = pd.Timestamp.now().tz_localize(None)
    print("[1/3] Lendo indicadores do Fundamentus...")
    ativos = carregar_fundamentus()
    print(f"[2/3] Enriquecendo {len(ativos)} ativos com Yahoo Finance...")
    resultado = []
    for i, ativo in enumerate(ativos, start=1):
        resultado.append(enriquecer_yahoo(ativo, agora))
        if i % 25 == 0:
            print(f"  processados {i}/{len(ativos)}")
        time.sleep(PAUSA_SEGUNDOS)
    documento = {"gerado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), "fonte": ["Fundamentus", "Yahoo Finance"], "total": len(resultado), "acoes": resultado}
    temporario = OUT.with_suffix(".json.tmp")
    temporario.write_text(json.dumps(documento, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    json.loads(temporario.read_text(encoding="utf-8"))
    temporario.replace(OUT)
    ok = sum(a.get("dados_yahoo") == "ok" for a in resultado)
    print(f"[3/3] Concluído: {len(resultado)} ativos; {ok} enriquecidos; {len(resultado)-ok} com Yahoo indisponível.")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERRO] Coleta não concluída: {exc}")
        raise
