"""Plan determinista de un vuelo: todos sus eventos, calculados de una vez.

El plan depende SOLO de la semilla y de la llave de negocio del vuelo. Por eso el
mundo simulado es reproducible aunque el generador se reinicie o vaya atrasado.

Eventos canónicos (la "verdad"):
  vuelo_creado, vuelo_cancelado, vuelo_cerrado, asiento_asignado, asiento_liberado

Los eventos con `inyectado` distinto de None son errores de negocio a propósito:
se publican en la fuente, se registran en la bitácora de errores y NO entran a la verdad.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .azar import hexa, rng, u
from .catalogo import ASIENTOS, Catalogo, VueloInstancia

ASIENTOS_INEXISTENTES = ["31A", "31C", "32B", "12G", "20H", "0A", "15K"]
SEG_DIA = 86400


@dataclass
class Evento:
    t: int                     # momento simulado (epoch UTC, segundos)
    orden: int                 # desempate dentro del vuelo
    evento_id: str
    tipo: str
    aerolinea: str
    numero_vuelo: str
    fecha_salida: str          # ISO, fecha local de salida
    asiento: str | None = None
    pasajero: str | None = None
    inyectado: str | None = None
    datos: dict = field(default_factory=dict)

    @property
    def llave(self) -> tuple[str, str, str]:
        return (self.aerolinea, self.numero_vuelo, self.fecha_salida)


def creacion(vi: VueloInstancia, semilla: int) -> int:
    """Momento simulado en que se crea el vuelo: entre 1 y 14 días antes de salir.

    La distribución se carga a los primeros días (media ~ 4.8 días) para que la
    simulación llegue pronto a régimen.
    """
    r = rng(semilla, "creacion", *vi.llave)
    dias = 1 + 13 * r.betavariate(1.3, 3.0)
    return vi.salida_utc - int(dias * SEG_DIA)


def opera(vi: VueloInstancia, semilla: int, fraccion: float) -> bool:
    return fraccion >= 1 or u(semilla, "opera", *vi.llave) < fraccion


def _numero_falso(numero: str, base: int) -> str:
    return f"{numero[:2]}{base + int(numero[2:]) % 1000}"


def planear(vi: VueloInstancia, semilla: int, tasa_cancelacion: float,
            negocio: dict[str, float], catalogo: Catalogo) -> list[Evento]:
    r = rng(semilla, "plan", *vi.llave)
    fecha = vi.fecha_salida.isoformat()
    creado = creacion(vi, semilla)
    cierre = vi.salida_utc - 3600
    usados: set[int] = {creado, cierre}
    eventos: list[Evento] = []

    def t_unico(t: int) -> int:
        while t in usados:
            t += 1
        usados.add(t)
        return t

    def nuevo(t: int, tipo: str, asiento=None, pasajero=None, inyectado=None, datos=None,
              numero=None) -> Evento:
        orden = len(eventos)
        ev = Evento(t, orden, f"{vi.aerolinea}-{hexa(semilla, 'ev', *vi.llave, orden)}", tipo,
                    vi.aerolinea, numero or vi.numero_vuelo, fecha, asiento, pasajero, inyectado,
                    datos or {})
        eventos.append(ev)
        return ev

    nuevo(creado, "vuelo_creado", datos=vi.atributos())

    cancelado_t = None
    if r.random() < tasa_cancelacion:
        cancelado_t = t_unico(creado + int((cierre - creado) * r.uniform(0.15, 0.97)))
    fin = cancelado_t or cierre

    ocupacion = r.uniform(vi.ocupacion_min, vi.ocupacion_max) / 100
    n_final = round(ocupacion * len(ASIENTOS))
    n_abandona = round(n_final * r.uniform(0.03, 0.08))
    n_cambia = round(n_final * r.uniform(0.02, 0.06))
    n_pax = n_final + n_abandona
    pasajeros = [hexa(semilla, "pax", *vi.llave, i) for i in range(n_pax)]
    span = cierre - creado

    acciones: list[tuple[int, str, int]] = []
    t_asigna: dict[int, int] = {}
    for i in range(n_pax):
        t = t_unico(creado + 60 + int((span - 120) * r.betavariate(2.2, 1.3)))
        t_asigna[i] = t
        acciones.append((t, "asignar", i))
    abandonan = set(r.sample(range(n_pax), n_abandona))
    for i in sorted(abandonan):
        ta = t_asigna[i]
        acciones.append((t_unico(ta + int((cierre - ta) * r.uniform(0.05, 0.9))), "liberar", i))
    candidatos = [i for i in range(n_pax) if i not in abandonan]
    for i in sorted(r.sample(candidatos, min(n_cambia, len(candidatos)))):
        ta = t_asigna[i]
        tc = t_unico(ta + int((cierre - ta) * r.uniform(0.05, 0.9)))
        acciones.append((tc, "cambiar", i))
        acciones.append((t_unico(tc + r.randint(5, 90)), "reasignar", i))
    acciones.sort()

    libres = set(ASIENTOS)
    asiento_de: dict[int, str] = {}
    desde: dict[str, int] = {}          # asiento -> momento de su asignación vigente
    for t, accion, i in acciones:
        if t >= fin:
            continue
        if accion in ("asignar", "reasignar"):
            if i in asiento_de or not libres:
                continue
            if accion == "reasignar" and i not in t_asigna:
                continue
            opciones = sorted(libres)
            asiento = r.choice(opciones)
            libres.discard(asiento)
            asiento_de[i] = asiento
            desde[asiento] = t
            nuevo(t, "asiento_asignado", asiento, pasajeros[i])
        else:  # liberar | cambiar
            asiento = asiento_de.pop(i, None)
            if asiento is None:
                continue
            libres.add(asiento)
            desde.pop(asiento, None)
            nuevo(t, "asiento_liberado", asiento, pasajeros[i])

    if cancelado_t is not None:
        nuevo(cancelado_t, "vuelo_cancelado", datos=vi.atributos())
    else:
        nuevo(cierre, "vuelo_cerrado", datos=vi.atributos())

    # --- Errores de negocio inyectados (no entran a la verdad) ---
    rn = rng(semilla, "negocio", *vi.llave)
    p = negocio or {}
    if desde and rn.random() < p.get("asiento_doble", 0):
        asiento = rn.choice(sorted(desde))
        ta = desde[asiento]
        t = t_unico(ta + max(1, int((fin - ta) * rn.uniform(0.2, 0.9))))
        if t < fin:
            nuevo(t, "asiento_asignado", asiento, hexa(semilla, "pax_falso", *vi.llave, "doble"),
                  inyectado="asiento_doble")
    if rn.random() < p.get("asiento_inexistente", 0):
        t = t_unico(creado + int((fin - creado) * rn.uniform(0.1, 0.95)))
        nuevo(t, "asiento_asignado", rn.choice(ASIENTOS_INEXISTENTES),
              hexa(semilla, "pax_falso", *vi.llave, "inexistente"), inyectado="asiento_inexistente")
    if rn.random() < p.get("asignacion_vuelo_no_abierto", 0):
        t = t_unico(fin + rn.randint(60, 2400))
        nuevo(t, "asiento_asignado", rn.choice(ASIENTOS),
              hexa(semilla, "pax_falso", *vi.llave, "no_abierto"), inyectado="asignacion_vuelo_no_abierto")
    if rn.random() < p.get("origen_igual_destino", 0):
        datos = vi.atributos() | {"destino": vi.origen, "llegada_local": vi.salida_local.isoformat(),
                                  "llegada_utc": vi.salida_utc}
        nuevo(t_unico(creado + rn.randint(30, 600)), "vuelo_creado", datos=datos,
              numero=_numero_falso(vi.numero_vuelo, 9000), inyectado="origen_igual_destino")
    if rn.random() < p.get("ruta_fuera_de_red", 0):
        red = catalogo.aerolineas[vi.aerolinea].aeropuertos
        fuera = sorted(set(catalogo.aeropuertos) - set(red))
        destino = rn.choice(fuera)
        dur = catalogo.duracion_min(vi.origen, destino) * 60
        llegada_utc = vi.salida_utc + dur
        from datetime import datetime, timezone
        llegada_local = datetime.fromtimestamp(llegada_utc, tz=timezone.utc).astimezone(
            catalogo.aeropuertos[destino].tz).replace(tzinfo=None)
        datos = vi.atributos() | {"destino": destino, "llegada_local": llegada_local.isoformat(),
                                  "llegada_utc": llegada_utc}
        nuevo(t_unico(creado + rn.randint(30, 600)), "vuelo_creado", datos=datos,
              numero=_numero_falso(vi.numero_vuelo, 8000), inyectado="ruta_fuera_de_red")

    eventos.sort(key=lambda e: (e.t, e.orden))
    return eventos
