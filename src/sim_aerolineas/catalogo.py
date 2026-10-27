"""Catálogo de aeropuertos, aerolíneas y vuelos programados (datos versionados en datos/)."""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .config import RAIZ

FILAS = range(1, 31)
LETRAS = "ABCDEF"
ASIENTOS = [f"{f}{l}" for f in FILAS for l in LETRAS]  # 180 asientos, mismo mapa para todos


@dataclass(frozen=True)
class Aeropuerto:
    iata: str
    icao: str
    nombre: str
    ciudad: str
    pais: str
    zona_horaria: str
    latitud: float
    longitud: float

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.zona_horaria)


@dataclass(frozen=True)
class Aerolinea:
    codigo: str
    letra: str
    nombre: str
    canal: str
    hubs: tuple[str, ...]
    tasa_cancelacion: float
    aeropuertos: frozenset[str]


@dataclass(frozen=True)
class VueloProgramado:
    aerolinea: str
    numero_vuelo: str
    origen: str
    destino: str
    hora_salida_local: str
    ocupacion_min: int
    ocupacion_max: int


@dataclass(frozen=True)
class VueloInstancia:
    """Un vuelo concreto: llave de negocio (aerolínea, número, fecha de salida local)."""
    aerolinea: str
    numero_vuelo: str
    fecha_salida: date            # fecha local en el aeropuerto de origen
    origen: str
    destino: str
    salida_local: datetime        # sin zona, hora del aeropuerto de origen
    llegada_local: datetime       # sin zona, hora del aeropuerto de destino
    salida_utc: int               # epoch, segundos
    llegada_utc: int
    ocupacion_min: int
    ocupacion_max: int

    @property
    def llave(self) -> tuple[str, str, str]:
        return (self.aerolinea, self.numero_vuelo, self.fecha_salida.isoformat())

    def atributos(self) -> dict:
        return {
            "origen": self.origen,
            "destino": self.destino,
            "salida_local": self.salida_local.isoformat(),
            "llegada_local": self.llegada_local.isoformat(),
            "salida_utc": self.salida_utc,
            "llegada_utc": self.llegada_utc,
        }


def distancia_km(a: Aeropuerto, b: Aeropuerto) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.latitud), math.radians(b.latitud)
    dp, dl = p2 - p1, math.radians(b.longitud - a.longitud)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


@dataclass
class Catalogo:
    aeropuertos: dict[str, Aeropuerto]
    aerolineas: dict[str, Aerolinea]
    programados: list[VueloProgramado]
    _por_icao: dict[str, Aeropuerto] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._por_icao = {a.icao: a for a in self.aeropuertos.values()}

    def por_icao(self, icao: str) -> Aeropuerto:
        return self._por_icao[icao]

    def duracion_min(self, origen: str, destino: str) -> int:
        """Tiempo de bloque: distancia a 800 km/h + 35 minutos de rodaje, redondeado a 5."""
        d = distancia_km(self.aeropuertos[origen], self.aeropuertos[destino])
        return int(round((d / 800 * 60 + 35) / 5) * 5)

    def instancia(self, vp: VueloProgramado, fecha: date) -> VueloInstancia:
        ao, ad = self.aeropuertos[vp.origen], self.aeropuertos[vp.destino]
        hh, mm = (int(x) for x in vp.hora_salida_local.split(":"))
        salida_local = datetime.combine(fecha, time(hh, mm))
        salida_utc = salida_local.replace(tzinfo=ao.tz).astimezone(timezone.utc)
        llegada_utc = salida_utc + timedelta(minutes=self.duracion_min(vp.origen, vp.destino))
        llegada_local = llegada_utc.astimezone(ad.tz).replace(tzinfo=None)
        return VueloInstancia(
            aerolinea=vp.aerolinea,
            numero_vuelo=vp.numero_vuelo,
            fecha_salida=fecha,
            origen=vp.origen,
            destino=vp.destino,
            salida_local=salida_local,
            llegada_local=llegada_local,
            salida_utc=int(salida_utc.timestamp()),
            llegada_utc=int(llegada_utc.timestamp()),
            ocupacion_min=vp.ocupacion_min,
            ocupacion_max=vp.ocupacion_max,
        )

    def vuelos_del_dia(self, fecha: date) -> list[VueloInstancia]:
        return [self.instancia(vp, fecha) for vp in self.programados]

    def red(self, aerolinea: str) -> set[tuple[str, str]]:
        return {(v.origen, v.destino) for v in self.programados if v.aerolinea == aerolinea}


@lru_cache(maxsize=4)
def cargar(raiz: Path = RAIZ) -> Catalogo:
    datos = Path(raiz) / "datos"
    with (datos / "aeropuertos.csv").open(encoding="utf-8") as f:
        aps = {
            r["iata"]: Aeropuerto(r["iata"], r["icao"], r["nombre"], r["ciudad"], r["pais"],
                                  r["zona_horaria"], float(r["latitud"]), float(r["longitud"]))
            for r in csv.DictReader(f)
        }
    cfg = yaml.safe_load((datos / "aerolineas.yaml").read_text(encoding="utf-8"))
    als = {
        a["codigo"]: Aerolinea(a["codigo"], a["letra"], a["nombre"], a["canal"], tuple(a["hubs"]),
                               float(a["tasa_cancelacion"]), frozenset(a["aeropuertos"]))
        for a in cfg["aerolineas"]
    }
    with (datos / "rutas.csv").open(encoding="utf-8") as f:
        progs = [
            VueloProgramado(r["aerolinea"], r["numero_vuelo"], r["origen"], r["destino"],
                            r["hora_salida_local"], int(r["ocupacion_min"]), int(r["ocupacion_max"]))
            for r in csv.DictReader(f)
        ]
    return Catalogo(aps, als, progs)
