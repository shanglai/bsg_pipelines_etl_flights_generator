"""Genera datos/rutas.csv a partir de datos/aerolineas.yaml (semilla 987654).

Se corre una sola vez; el CSV resultante se versiona en el repo. Si cambias las
redes, vuelve a correrlo y revisa el diff.

    python herramientas/generar_rutas.py
"""
from __future__ import annotations

import csv
import random
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[1]
SEMILLA = 987654


def horarios(rng: random.Random, n: int) -> list[str]:
    """n horas de salida entre 06:00 y 22:30, separadas al menos 3 horas."""
    while True:
        minutos = sorted(rng.randrange(6 * 60, 22 * 60 + 31, 5) for _ in range(n))
        if all(b - a >= 180 for a, b in zip(minutos, minutos[1:])):
            return [f"{m // 60:02d}:{m % 60:02d}" for m in minutos]


def main() -> None:
    rng = random.Random(SEMILLA)
    cfg = yaml.safe_load((RAIZ / "datos/aerolineas.yaml").read_text(encoding="utf-8"))
    filas = []
    for a in cfg["aerolineas"]:
        hub1 = a["hubs"][0]
        pares: list[tuple[str, str]] = []
        for ap in a["aeropuertos"]:
            if ap != hub1:
                pares.append((hub1, ap))
        if len(a["hubs"]) > 1:
            hub2 = a["hubs"][1]
            for ap in a["rutas_segundo_hub"]:
                if ap not in (hub1, hub2):
                    pares.append((hub2, ap))
        numero = 100
        for o, d in pares:
            # Frecuencia diaria: más vuelos entre hubs y a destinos grandes.
            es_troncal = d in a["hubs"] or o in a["hubs"] and d in ("MEX", "GDL", "MTY", "CUN", "TIJ")
            freq = rng.choice([2, 3]) if es_troncal else rng.choice([1, 1, 2])
            occ_min = rng.randrange(55, 81)
            occ_max = min(95, occ_min + rng.randrange(8, 20))
            for origen, destino in ((o, d), (d, o)):
                for h in horarios(rng, freq):
                    filas.append({
                        "aerolinea": a["codigo"],
                        "numero_vuelo": f"{a['codigo']}{numero}",
                        "origen": origen,
                        "destino": destino,
                        "hora_salida_local": h,
                        "ocupacion_min": occ_min,
                        "ocupacion_max": occ_max,
                    })
                    numero += 1
            numero = (numero // 10 + 1) * 10  # bloque nuevo por par de rutas
    destino = RAIZ / "datos/rutas.csv"
    with destino.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(filas)
    print(f"{len(filas)} vuelos diarios programados -> {destino}")


if __name__ == "__main__":
    main()
