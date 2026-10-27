"""Generador: hace avanzar el mundo simulado y publica en las tres fuentes.

Cada ciclo (~1 s real):
1. nacer: los vuelos cuyo momento de creación ya pasó entran a la agenda con su plan completo.
2. agenda: los eventos cuyo momento simulado ya pasó se aplican a la verdad y se encolan
   para su fuente (tabla simulador.salida).
3. publicar: API (A), MySQL (C) y SFTP (B), cada una con su latencia y sus errores.
4. mantenimiento (cada 5 min): retención, equipos nuevos, limpieza.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pymysql

from . import bd, catalogo, config, formatos, incidentes, plan
from .azar import u
from .reloj import Reloj, dt_utc, parsear_inicio

log = logging.getLogger("generador")
CANAL_A_FUENTE = {"api": "A", "sftp": "B", "mysql": "C"}


def _json(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def configurar_logs() -> None:
    class Json(logging.Formatter):
        def format(self, r: logging.LogRecord) -> str:
            d = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "nivel": r.levelname,
                 "componente": r.name, "mensaje": r.getMessage()}
            if isinstance(r.args, dict):
                d.update(r.args)
            extra = getattr(r, "datos", None)
            if extra:
                d.update(extra)
            return json.dumps(d, ensure_ascii=False, default=str)

    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(Json())
    logging.basicConfig(level=os.environ.get("SIM_LOG", "INFO"), handlers=[h], force=True)


def obtener_reloj(cn, cfg: dict, crear: bool = False) -> Reloj | None:
    with cn.cursor() as cur:
        cur.execute("SELECT * FROM simulador.reloj WHERE id=1")
        f = cur.fetchone()
        if f is None and crear:
            ahora = time.time()
            f = {"inicio_real": ahora, "inicio_sim": parsear_inicio(cfg["reloj"]["inicio_simulado"], ahora),
                 "factor": float(cfg["reloj"]["factor"]), "semilla": cfg["semilla"]}
            cur.execute("INSERT INTO simulador.reloj (id, inicio_real, inicio_sim, factor, semilla) "
                        "VALUES (1,%s,%s,%s,%s)", (f["inicio_real"], f["inicio_sim"], f["factor"], f["semilla"]))
            cn.commit()
    if f is None:
        return None
    return Reloj(float(f["inicio_real"]), float(f["inicio_sim"]), float(f["factor"]))


def asegurar_usuario_mysql(cn, usuario: str, contrasena: str, tls: bool) -> None:
    """Usuario de solo lectura sobre aerolinea_c, con privilegios para leer el binlog (CDC)."""
    req = " REQUIRE SSL" if tls else ""
    with cn.cursor() as cur:
        cur.execute(f"CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s{req}", (usuario, contrasena))
        cur.execute(f"ALTER USER %s@'%%' IDENTIFIED BY %s{req}", (usuario, contrasena))
        cur.execute("GRANT SELECT ON aerolinea_c.* TO %s@'%%'", (usuario,))
        cur.execute("GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO %s@'%%'", (usuario,))
    cn.commit()


class Generador:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or config.actual()
        self.semilla = int(self.cfg["semilla"])
        self.err = self.cfg["errores"]
        self.cat = catalogo.cargar()
        self.cn = bd.conectar(binlog=False)       # esquema interno: fuera del binlog
        self.cn_c = bd.conectar(binlog=True)      # fuente C: sí va al binlog (CDC)
        bd.crear_esquemas(self.cn, self.cn_c)
        self.reloj = obtener_reloj(self.cn, self.cfg, crear=True)
        self.fuente = {c: CANAL_A_FUENTE[a.canal] for c, a in self.cat.aerolineas.items()}
        self.codigo_por_fuente = {v: k for k, v in self.fuente.items()}
        self.origen_por_numero = {vp.numero_vuelo: vp.origen for vp in self.cat.programados}
        self.nacidos: set[tuple[str, str, str]] = set()
        self.cache_dias: dict[date, list] = {}
        self.rng = random.Random(self.semilla)
        self.proximo_lote = 0.0
        self.contadores: Counter = Counter()
        self.incidentes: list[dict] = []
        self.vuelo_id_c: dict[tuple[str, str], int] = {}
        self.sftp_dir = Path(os.environ.get("SFTP_DIR", "/datos/sftp"))
        self.usuarios_sftp: list[str] = []
        self.ultimo_mant = 0.0
        self.ultimo_reporte = 0.0
        self._cargar_estado()
        self._mantenimiento(time.time())

    # ------------------------------------------------------------------ arranque
    def _cargar_estado(self) -> None:
        hoy = datetime.fromtimestamp(self.reloj.ahora_sim(), tz=timezone.utc).date()
        with self.cn.cursor() as cur:
            cur.execute("SELECT aerolinea, numero_vuelo, fecha_salida FROM simulador.nacidos "
                        "WHERE fecha_salida >= %s", (hoy - timedelta(days=4),))
            self.nacidos = {(f["aerolinea"], f["numero_vuelo"], f["fecha_salida"].isoformat())
                            for f in cur.fetchall()}
        cada = self.cfg["latencia"]["sftp"]["cada"]
        v = bd.leer_estado(self.cn, "ultima_ventana_b")
        if v is None:
            v = int(time.time() // cada) - 1
            bd.escribir_estado(self.cn, "ultima_ventana_b", v)
            bd.escribir_estado(self.cn, "ventana_b_inicial", v + 1)
        self.ultima_ventana_b = int(v)
        self.ventana_b_inicial = int(bd.leer_estado(self.cn, "ventana_b_inicial", v))
        self.cn.commit()
        local_usr = os.environ.get("ALUMNO_MYSQL_USUARIO")
        if local_usr:
            asegurar_usuario_mysql(self.cn, local_usr, os.environ.get("ALUMNO_MYSQL_CONTRASENA", ""),
                                   os.environ.get("MYSQL_TLS", "0") == "1")
        log.info("generador listo", extra={"datos": {
            "modo": self.cfg["modo"], "factor": self.reloj.factor, "semilla": self.semilla,
            "inicio_sim": datetime.fromtimestamp(self.reloj.inicio_sim, tz=timezone.utc).isoformat(),
            "errores_activos": self.err.get("activos", True), "vuelos_nacidos_cargados": len(self.nacidos)}})

    # ------------------------------------------------------------------ ciclo
    def correr(self) -> None:
        while True:
            t0 = time.time()
            try:
                self.ciclo()
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as e:
                log.warning("se perdió la conexión a MySQL; reconecto", extra={"datos": {"error": str(e)}})
                time.sleep(3)
                self.cn = bd.conectar(binlog=False)
                self.cn_c = bd.conectar(binlog=True)
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))

    def ciclo(self) -> None:
        ahora = time.time()
        sim = self.reloj.sim(ahora)
        self.incidentes = incidentes.activos(self.cn, ahora)
        self._programados(ahora)
        self.nacer(sim)
        n = self.procesar_agenda(sim)
        self.publicar_api(ahora)
        self.publicar_mysql(ahora)
        self.publicar_sftp(ahora)
        if ahora - self.ultimo_mant > 300:
            self._mantenimiento(ahora)
        bd.sumar_contadores(self.cn, self.contadores)
        self.contadores.clear()
        bd.escribir_estado(self.cn, "latido", f"{ahora:.3f}")
        bd.escribir_estado(self.cn, "sim_ahora", f"{sim:.0f}")
        self.cn.commit()
        if ahora - self.ultimo_reporte > 60:
            self.ultimo_reporte = ahora
            log.info("ciclo", extra={"datos": {
                "sim": datetime.fromtimestamp(sim, tz=timezone.utc).isoformat(timespec="minutes"),
                "eventos_ultimo_ciclo": n, "incidentes_activos": [i["tipo"] for i in self.incidentes]}})

    # ------------------------------------------------------------------ nacer
    def nacer(self, sim: float) -> None:
        hoy = datetime.fromtimestamp(sim, tz=timezone.utc).date()
        fraccion = float(self.cfg["volumen"]["fraccion_vuelos"])
        inicio_mundo = self.reloj.inicio_sim - 3600 * float(self.cfg["reloj"].get("precarga_horas", 0))
        nuevos = []
        for d in range(-2, 16):
            f = hoy + timedelta(days=d)
            if f not in self.cache_dias:
                lst = [(plan.creacion(vi, self.semilla), vi) for vi in self.cat.vuelos_del_dia(f)
                       if plan.opera(vi, self.semilla, fraccion)]
                lst.sort(key=lambda x: x[0])
                self.cache_dias[f] = lst
            for creado, vi in self.cache_dias[f]:
                if creado > sim:
                    break
                if creado >= inicio_mundo and vi.llave not in self.nacidos:
                    nuevos.append(vi)
        for f in [f for f in self.cache_dias if f < hoy - timedelta(days=3)]:
            del self.cache_dias[f]
        if not nuevos:
            return
        filas_agenda, filas_nac = [], []
        for vi in nuevos:
            a = self.cat.aerolineas[vi.aerolinea]
            evs = plan.planear(vi, self.semilla, a.tasa_cancelacion, self.err.get("negocio", {}), self.cat)
            for e in evs:
                filas_agenda.append((e.t, e.orden, e.evento_id, e.aerolinea, e.numero_vuelo, e.fecha_salida,
                                     e.tipo, e.asiento, e.pasajero, e.inyectado, _json(e.datos) if e.datos else None))
            filas_nac.append((*vi.llave, evs[0].t))
        with self.cn.cursor() as cur:
            for i in range(0, len(filas_agenda), 5000):
                cur.executemany(
                    "INSERT INTO simulador.agenda (t_sim, orden, evento_id, aerolinea, numero_vuelo, fecha_salida, "
                    "tipo, asiento, pasajero, inyectado, datos) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    filas_agenda[i:i + 5000])
            cur.executemany("INSERT IGNORE INTO simulador.nacidos VALUES (%s,%s,%s,%s)", filas_nac)
        self.cn.commit()
        self.nacidos.update(vi.llave for vi in nuevos)
        self.contadores["vuelos.nacidos"] += len(nuevos)

    # ------------------------------------------------------------------ agenda -> verdad + colas
    def procesar_agenda(self, sim: float) -> int:
        ahora = time.time()
        with self.cn.cursor() as cur:
            cur.execute("SELECT * FROM simulador.agenda WHERE t_sim <= %s ORDER BY t_sim, orden, id LIMIT 20000",
                        (int(sim),))
            filas = cur.fetchall()
            if not filas:
                return 0
            verdad, salida, errores = [], [], []
            rafaga = next((i for i in self.incidentes if i["tipo"] == "rafaga_duplicados"), None)
            for f in filas:
                ev = {"evento_id": f["evento_id"], "t": int(f["t_sim"]), "orden": f["orden"], "tipo": f["tipo"],
                      "aerolinea": f["aerolinea"], "numero_vuelo": f["numero_vuelo"],
                      "fecha_salida": f["fecha_salida"].isoformat(), "asiento": f["asiento"],
                      "pasajero": f["pasajero"], "datos": json.loads(f["datos"]) if f["datos"] else {}}
                fuente = self.fuente[ev["aerolinea"]]
                if f["inyectado"]:
                    errores.append((fuente, f["inyectado"], ev["aerolinea"], ev["numero_vuelo"], ev["fecha_salida"],
                                    ev["evento_id"], _json({"tipo_evento": ev["tipo"], "asiento": ev["asiento"],
                                                            "datos": ev["datos"]}), ahora, ev["t"]))
                    self.contadores[f"negocio.{f['inyectado']}"] += 1
                else:
                    self._aplicar_verdad(cur, ev)
                    verdad.append((ev["evento_id"], ev["aerolinea"], ev["numero_vuelo"], ev["fecha_salida"],
                                   ev["tipo"], ev["asiento"], ev["pasajero"], ev["t"], ev["orden"],
                                   self.reloj.real(ev["t"])))
                salida.extend(self._encolar(ev, fuente, errores, rafaga))
            cur.executemany("INSERT IGNORE INTO simulador.verdad_eventos VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            verdad)
            cur.executemany("INSERT INTO simulador.salida (fuente, evento_id, evento, ocurrido_real, publicar_en, "
                            "ventana, duplicado) VALUES (%s,%s,%s,%s,%s,%s,%s)", salida)
            self._registrar_errores(cur, errores)
            ids = [f["id"] for f in filas]
            for i in range(0, len(ids), 5000):
                lote = ids[i:i + 5000]
                cur.execute(f"DELETE FROM simulador.agenda WHERE id IN ({','.join(['%s'] * len(lote))})", lote)
        self.cn.commit()
        self.contadores["verdad.eventos"] += len(verdad)
        return len(filas)

    def _aplicar_verdad(self, cur, ev: dict) -> None:
        k = (ev["aerolinea"], ev["numero_vuelo"], ev["fecha_salida"])
        t = ev["tipo"]
        if t == "vuelo_creado":
            d = ev["datos"]
            cur.execute("INSERT IGNORE INTO simulador.verdad_vuelos (aerolinea, numero_vuelo, fecha_salida, origen, "
                        "destino, salida_local, llegada_local, salida_utc, llegada_utc, estado, creado_sim) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'programado',%s)",
                        (*k, d["origen"], d["destino"], d["salida_local"], d["llegada_local"], d["salida_utc"],
                         d["llegada_utc"], ev["t"]))
        elif t == "vuelo_cancelado":
            cur.execute("UPDATE simulador.verdad_vuelos SET estado='cancelado', cancelado_sim=%s "
                        "WHERE aerolinea=%s AND numero_vuelo=%s AND fecha_salida=%s", (ev["t"], *k))
            cur.execute("DELETE FROM simulador.verdad_asientos WHERE aerolinea=%s AND numero_vuelo=%s "
                        "AND fecha_salida=%s", k)
        elif t == "vuelo_cerrado":
            cur.execute("UPDATE simulador.verdad_vuelos SET estado='cerrado', cerrado_sim=%s "
                        "WHERE aerolinea=%s AND numero_vuelo=%s AND fecha_salida=%s", (ev["t"], *k))
        elif t == "asiento_asignado":
            cur.execute("INSERT INTO simulador.verdad_asientos VALUES (%s,%s,%s,%s,%s,%s)",
                        (*k, ev["asiento"], ev["pasajero"], ev["t"]))
        elif t == "asiento_liberado":
            cur.execute("DELETE FROM simulador.verdad_asientos WHERE aerolinea=%s AND numero_vuelo=%s AND "
                        "fecha_salida=%s AND asiento=%s AND pasajero=%s", (*k, ev["asiento"], ev["pasajero"]))

    def _encolar(self, ev: dict, fuente: str, errores: list, rafaga: dict | None) -> list[tuple]:
        ocurrido = self.reloj.real(ev["t"])
        evj = _json(ev)
        eid = ev["evento_id"]
        if fuente == "A":
            lat = self.cfg["latencia"]["api"]
            p = self.err.get("api", {})
            if u(self.semilla, "api_tarde", eid) < p.get("tarde", 0):
                demora = lat["tarde_min"] + (lat["tarde_max"] - lat["tarde_min"]) * u(self.semilla, "api_lat2", eid)
                errores.append(("A", "evento_tarde", ev["aerolinea"], ev["numero_vuelo"], ev["fecha_salida"], eid,
                                _json({"demora_s": round(demora, 1)}), time.time(), ev["t"]))
            else:
                demora = lat["min"] + (lat["max"] - lat["min"]) * u(self.semilla, "api_lat", eid)
            filas = [("A", eid, evj, ocurrido, ocurrido + demora, None, 0)]
            dup = u(self.semilla, "dup", eid) < p.get("duplicado", 0)
            por_rafaga = bool(rafaga) and u(self.semilla, "rafaga", eid) < float(rafaga["parametros"].get("proporcion", 0.3))
            if dup or por_rafaga:
                filas.append(("A", eid, evj, ocurrido, ocurrido + demora + 1 + 39 * u(self.semilla, "dup_lat", eid), None, 1))
                errores.append(("A", "duplicado", ev["aerolinea"], ev["numero_vuelo"], ev["fecha_salida"], eid,
                                _json({"por_rafaga": por_rafaga}), time.time(), ev["t"]))
            return filas
        if fuente == "B":
            ventana = max(int(ocurrido // self.cfg["latencia"]["sftp"]["cada"]), self.ultima_ventana_b + 1)
            return [("B", eid, evj, ocurrido, ocurrido, ventana, 0)]
        return [("C", eid, evj, ocurrido, ocurrido, None, 0)]

    @staticmethod
    def _registrar_errores(cur, errores: list) -> None:
        if errores:
            cur.executemany("INSERT INTO simulador.errores_inyectados (fuente, tipo, aerolinea, numero_vuelo, "
                            "fecha_salida, referencia, detalle, real_en, sim_en) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            errores)

    # ------------------------------------------------------------------ fuente A: API
    def _version_api(self, eid: str, ahora: float) -> int:
        cambio = [i for i in self.incidentes if i["tipo"] == "cambio_esquema_api"]
        if not cambio:
            return 1
        c = cambio[0]
        if ahora < c["inicio_real"] + 60 * float(c["parametros"].get("convivencia_min", 30)):
            return 2 if u(self.semilla, "v2", eid) < 0.5 else 1
        return 2

    def publicar_api(self, ahora: float) -> None:
        p_off = self.err.get("api", {}).get("offset_local", 0)
        with self.cn.cursor() as cur:
            cur.execute("SELECT id, evento_id, evento FROM simulador.salida WHERE fuente='A' AND publicar_en <= %s "
                        "ORDER BY publicar_en, id LIMIT 5000", (ahora,))
            filas = cur.fetchall()
            if not filas:
                return
            pub = []
            for f in filas:
                ev = json.loads(f["evento"])
                v = self._version_api(f["evento_id"], ahora)
                off = u(self.semilla, "offset", f["evento_id"]) < p_off
                pub.append((f["evento_id"], v, _json(formatos.evento_api(ev, v, off, self.semilla)), ahora))
                self.contadores["api.publicados"] += 1
                self.contadores["api.offset_local"] += off
                self.contadores["api.v2"] += v == 2
            cur.executemany("INSERT INTO simulador.api_eventos (id_evento, version, payload, publicado_en) "
                            "VALUES (%s,%s,%s,%s)", pub)
            ids = [f["id"] for f in filas]
            cur.execute(f"DELETE FROM simulador.salida WHERE id IN ({','.join(['%s'] * len(ids))})", ids)
        self.cn.commit()

    # ------------------------------------------------------------------ fuente C: MySQL
    def publicar_mysql(self, ahora: float) -> None:
        if any(i["tipo"] == "pausa_c" for i in self.incidentes):
            self._en_pausa = True
            return
        if getattr(self, "_en_pausa", False):       # terminó una pausa: ráfaga inmediata
            self._en_pausa = False
            self.proximo_lote = 0
        if ahora < self.proximo_lote:
            return
        lat = self.cfg["latencia"]["mysql"]
        self.proximo_lote = ahora + self.rng.uniform(lat["lote_min"], lat["lote_max"])
        cn = self.cn_c
        errores = []
        with cn.cursor() as cur:
            cur.execute("SELECT id, evento_id, evento, publicar_en FROM simulador.salida WHERE fuente='C' ORDER BY id")
            pendientes = cur.fetchall()
            if not pendientes:
                cn.commit()
                return
            for f in pendientes:
                f["ev"] = json.loads(f["evento"])
                f["llave"] = (f["ev"]["numero_vuelo"], f["ev"]["fecha_salida"])
            listos = [f for f in pendientes if f["publicar_en"] <= ahora]
            bloqueados = {f["llave"] for f in pendientes if f["publicar_en"] > ahora}
            p_larga = self.err.get("mysql", {}).get("transaccion_larga", 0)
            if listos and self.rng.random() < p_larga:
                llaves = sorted({f["llave"] for f in listos} - bloqueados)
                if llaves:
                    elegidas = set(self.rng.sample(llaves, max(1, round(len(llaves) * 0.25))))
                    hasta = ahora + self.rng.uniform(lat["larga_min"], lat["larga_max"])
                    ids = [f["id"] for f in listos if f["llave"] in elegidas]
                    cur.execute(f"UPDATE simulador.salida SET retenido=1, publicar_en=%s WHERE id IN "
                                f"({','.join(['%s'] * len(ids))})", [hasta, *ids])
                    bloqueados |= elegidas
                    errores.append(("C", "transaccion_larga", self.codigo_por_fuente["C"], None, None, None,
                                    _json({"vuelos": len(elegidas), "eventos": len(ids),
                                           "confirma_en_s": round(hasta - ahora, 1)}),
                                    ahora, int(self.reloj.sim(ahora))))
            aplicar = [f for f in listos if f["llave"] not in bloqueados]
            for f in aplicar:
                self._aplicar_c(cur, f["ev"], errores, ahora)
            self._registrar_errores(cur, errores)
            if aplicar:
                ids = [f["id"] for f in aplicar]
                for i in range(0, len(ids), 5000):
                    lote = ids[i:i + 5000]
                    cur.execute(f"DELETE FROM simulador.salida WHERE id IN ({','.join(['%s'] * len(lote))})", lote)
        cn.commit()
        self.contadores["mysql.lotes"] += 1
        self.contadores["mysql.eventos"] += len(aplicar)

    def _vuelo_c(self, cur, numero: str, fecha: str) -> int | None:
        k = (numero, fecha)
        if k not in self.vuelo_id_c:
            cur.execute("SELECT id FROM aerolinea_c.vuelos WHERE numero_vuelo=%s AND fecha_salida=%s ORDER BY id LIMIT 1",
                        k)
            f = cur.fetchone()
            if not f:
                return None
            self.vuelo_id_c[k] = f["id"]
        return self.vuelo_id_c[k]

    def _aplicar_c(self, cur, ev: dict, errores: list, ahora: float) -> None:
        t, tipo = ev["t"], ev["tipo"]
        ts = dt_utc(t)
        if tipo == "vuelo_creado":
            d = ev["datos"]
            cur.execute("INSERT INTO aerolinea_c.vuelos (numero_vuelo, fecha_salida, origen, destino, salida_local, "
                        "llegada_local, estado, creado_en, actualizado_en) VALUES (%s,%s,%s,%s,%s,%s,'ABIERTO',%s,%s)",
                        (ev["numero_vuelo"], ev["fecha_salida"], self.cat.aeropuertos[d["origen"]].icao,
                         self.cat.aeropuertos[d["destino"]].icao, d["salida_local"], d["llegada_local"], ts, ts))
            self.vuelo_id_c[(ev["numero_vuelo"], ev["fecha_salida"])] = cur.lastrowid
            return
        vid = self._vuelo_c(cur, ev["numero_vuelo"], ev["fecha_salida"])
        if vid is None:
            self.contadores["mysql.sin_vuelo"] += 1
            return
        if tipo == "vuelo_cancelado":
            cur.execute("UPDATE aerolinea_c.vuelos SET estado='CANCELADO', actualizado_en=%s WHERE id=%s", (ts, vid))
            cur.execute("DELETE FROM aerolinea_c.asignaciones WHERE vuelo_id=%s", (vid,))
        elif tipo == "vuelo_cerrado":
            if u(self.semilla, "correccion", ev["evento_id"]) < self.err.get("mysql", {}).get("correccion_manual", 0):
                cur.execute("UPDATE aerolinea_c.vuelos SET estado='CERRADO' WHERE id=%s", (vid,))
                errores.append(("C", "correccion_manual", ev["aerolinea"], ev["numero_vuelo"], ev["fecha_salida"],
                                f"vuelos.id={vid}", _json({"estado": "CERRADO"}), ahora, t))
            else:
                cur.execute("UPDATE aerolinea_c.vuelos SET estado='CERRADO', actualizado_en=%s WHERE id=%s", (ts, vid))
        elif tipo == "asiento_asignado":
            cur.execute("INSERT INTO aerolinea_c.asignaciones (vuelo_id, asiento, pasajero, asignado_en, actualizado_en) "
                        "VALUES (%s,%s,%s,%s,%s)", (vid, ev["asiento"], ev["pasajero"], ts, ts))
        elif tipo == "asiento_liberado":
            cur.execute("DELETE FROM aerolinea_c.asignaciones WHERE vuelo_id=%s AND asiento=%s AND pasajero=%s",
                        (vid, ev["asiento"], ev["pasajero"]))

    # ------------------------------------------------------------------ fuente B: SFTP
    def _nombre_b(self, tipo: str, ventana: int) -> str:
        cada = self.cfg["latencia"]["sftp"]["cada"]
        fin_sim = datetime.fromtimestamp(self.reloj.sim((ventana + 1) * cada), tz=timezone.utc)
        lote = ventana - self.ventana_b_inicial
        return f"{self.codigo_por_fuente['B']}_{tipo}_{lote:06d}_{fin_sim:%Y%m%dT%H%M}.csv"

    def _formato_b(self, nombre: str) -> tuple[str, str]:
        p = self.err.get("sftp", {})
        cod = "cp1252" if u(self.semilla, "cod", nombre) < p.get("windows_1252", 0) else "utf-8"
        sep = ";" if u(self.semilla, "sep", nombre) < p.get("punto_y_coma", 0) else ","
        return cod, sep

    def _contenido_b(self, a: dict) -> tuple[bytes, int, list]:
        """Contenido de un archivo (determinista para nombre+versión). Regresa bytes, filas y errores."""
        with self.cn.cursor() as cur:
            cur.execute("SELECT evento FROM simulador.salida WHERE fuente='B' AND ventana=%s ORDER BY id", (a["ventana"],))
            eventos = [json.loads(f["evento"]) for f in cur.fetchall()]
        cada = self.cfg["latencia"]["sftp"]["cada"]
        if a["tipo"] == "vuelos":
            filas = formatos.filas_b_vuelos(eventos, self.cat, self.origen_por_numero)
            columnas = formatos.COLUMNAS_B_VUELOS
        else:
            desde = float(self.cfg["programado"]["sftp_columna_nueva_desde_hora"])
            con_clase = self.reloj.horas_simuladas((a["ventana"] + 1) * cada) >= desde
            filas = formatos.filas_b_asientos(eventos, self.cat, self.origen_por_numero, self.semilla, con_clase)
            columnas = formatos.COLUMNAS_B_ASIENTOS_NUEVA if con_clase else formatos.COLUMNAS_B_ASIENTOS
        if a.get("defecto") == "reenvio" and filas:
            r = random.Random(f"{self.semilla}|reenvio|{a['nombre']}")
            quitar = set(r.sample(range(len(filas)), min(len(filas), r.randint(1, 3))))
            filas = [f for i, f in enumerate(filas) if i not in quitar]
        return formatos.csv_bytes(filas, columnas, a["separador"], a["codificacion"]), len(filas), eventos

    def _dirs_usuarios(self) -> list[Path]:
        res = []
        for usr in self.usuarios_sftp:
            d = self.sftp_dir / usr / "salida"
            if not d.is_dir():
                d.mkdir(parents=True, exist_ok=True)
                os.chmod(d.parent, 0o755)
                os.chmod(d, 0o755)
            res.append(d)
        return res

    def _vincular(self, origen: Path, nombre: str) -> None:
        for d in self._dirs_usuarios():
            tmp = d / f".{nombre}.lnk"
            if tmp.exists():
                tmp.unlink()
            os.link(origen, tmp)
            os.replace(tmp, d / nombre)

    def publicar_sftp(self, ahora: float) -> None:
        s = self.cfg["latencia"]["sftp"]
        p = self.err.get("sftp", {})
        cada = s["cada"]
        maestro = self.sftp_dir / "_maestro"
        maestro.mkdir(parents=True, exist_ok=True)
        errores = []
        with self.cn.cursor() as cur:
            # 1) Programar las ventanas que ya terminaron.
            actual = int(ahora // cada)
            while self.ultima_ventana_b < actual - 1:
                w = self.ultima_ventana_b + 1
                fin_w = (w + 1) * cada
                tarde = u(self.semilla, "sftp_tarde", w) < p.get("tarde", 0)
                if tarde:
                    prog = fin_w + s["tarde_min"] + (s["tarde_max"] - s["tarde_min"]) * u(self.semilla, "sftp_tlat", w)
                else:
                    prog = fin_w + 5 + 35 * u(self.semilla, "sftp_lat", w)
                for tipo in ("vuelos", "asientos"):
                    nombre = self._nombre_b(tipo, w)
                    cod, sep = self._formato_b(nombre)
                    cur.execute("INSERT INTO simulador.sftp_archivos (nombre, ventana, tipo, version, estado, "
                                "programado_en, codificacion, separador) VALUES (%s,%s,%s,1,'programado',%s,%s,%s)",
                                (nombre, w, tipo, prog, cod, sep))
                    if tarde:
                        errores.append(("B", "archivo_tarde", self.codigo_por_fuente["B"], None, None, nombre,
                                        _json({"demora_s": round(prog - fin_w, 1)}), ahora, int(self.reloj.sim(ahora))))
                    if cod != "utf-8" or sep != ",":
                        errores.append(("B", "formato_distinto", self.codigo_por_fuente["B"], None, None, nombre,
                                        _json({"codificacion": cod, "separador": sep}), ahora,
                                        int(self.reloj.sim(ahora))))
                self.ultima_ventana_b = w
                bd.escribir_estado(self.cn, "ultima_ventana_b", w)
            self._registrar_errores(cur, errores)
            self.cn.commit()
            errores = []

            # 2) Archivos que toca empezar a escribir.
            cur.execute("SELECT * FROM simulador.sftp_archivos WHERE estado='programado' AND programado_en <= %s "
                        "ORDER BY id", (ahora,))
            for a in cur.fetchall():
                inc = next((i for i in self.incidentes if i["tipo"] == "sftp_codificacion"), None)
                if inc and a["version"] == 1:
                    a["codificacion"] = inc["parametros"].get("codificacion", "utf-16")
                    usados = int(inc["parametros"].get("usados", 0)) + 1
                    inc["parametros"]["usados"] = usados
                    fin = ahora if usados >= int(inc["parametros"].get("archivos", 2)) else None
                    cur.execute("UPDATE simulador.incidentes SET parametros=%s, fin_real=%s WHERE id=%s",
                                (_json(inc["parametros"]), fin, inc["id"]))
                    if fin:
                        self.incidentes = [i for i in self.incidentes if i["id"] != inc["id"]]
                    errores.append(("B", "codificacion_incidente", self.codigo_por_fuente["B"], None, None, a["nombre"],
                                    _json({"codificacion": a["codificacion"], "incidente": inc["id"]}), ahora,
                                    int(self.reloj.sim(ahora))))
                defecto = None
                if a["version"] == 1:
                    if u(self.semilla, "truncado", a["nombre"]) < p.get("truncado", 0):
                        defecto = "truncado"
                    elif u(self.semilla, "reenvio", a["nombre"]) < p.get("reenvio_corregido", 0):
                        defecto = "reenvio"
                a["defecto"] = defecto
                contenido, nfilas, _ = self._contenido_b(a)
                if defecto == "truncado":
                    corte = int(len(contenido) * (0.3 + 0.6 * u(self.semilla, "corte", a["nombre"])))
                    destino = maestro / a["nombre"]
                    tmp = maestro / f".{a['nombre']}.trunc"
                    tmp.write_bytes(contenido[:corte])
                    os.replace(tmp, destino)
                    self._vincular(destino, a["nombre"])
                    cur.execute("UPDATE simulador.sftp_archivos SET estado='escrito', escrito_en=%s, defecto=%s, "
                                "codificacion=%s, bytes=%s, filas=%s WHERE id=%s",
                                (ahora, defecto, a["codificacion"], corte, nfilas, a["id"]))
                else:
                    tmp = maestro / f"{a['nombre']}.tmp"
                    tmp.write_bytes(contenido[: max(1, len(contenido) * 2 // 5)])   # a medio escribir
                    self._vincular(tmp, f"{a['nombre']}.tmp")
                    renombrar = ahora + 3 + 12 * u(self.semilla, "renombrar", a["nombre"], a["version"])
                    cur.execute("UPDATE simulador.sftp_archivos SET estado='escribiendo', renombrar_en=%s, defecto=%s, "
                                "codificacion=%s WHERE id=%s", (renombrar, defecto, a["codificacion"], a["id"]))
                if defecto:
                    s2 = ahora + s["correccion_min"] + (s["correccion_max"] - s["correccion_min"]) * u(
                        self.semilla, "corrige", a["nombre"])
                    cur.execute("INSERT INTO simulador.sftp_archivos (nombre, ventana, tipo, version, estado, "
                                "programado_en, codificacion, separador) VALUES (%s,%s,%s,%s,'programado',%s,%s,%s)",
                                (a["nombre"], a["ventana"], a["tipo"], a["version"] + 1, s2, a["codificacion"],
                                 a["separador"]))
                    errores.append(("B", "archivo_truncado" if defecto == "truncado" else "reenvio_corregido",
                                    self.codigo_por_fuente["B"], None, None, a["nombre"],
                                    _json({"version_corregida_en_s": round(s2 - ahora, 1)}), ahora,
                                    int(self.reloj.sim(ahora))))
            self._registrar_errores(cur, errores)
            self.cn.commit()

            # 3) Terminar de escribir y renombrar (atómico) los que estaban a medias.
            cur.execute("SELECT * FROM simulador.sftp_archivos WHERE estado='escribiendo' AND renombrar_en <= %s "
                        "ORDER BY id", (ahora,))
            for a in cur.fetchall():
                contenido, nfilas, _ = self._contenido_b(a)
                tmp = maestro / f"{a['nombre']}.tmp"
                with open(tmp, "wb") as fh:          # mismo inodo: los enlaces de cada equipo ven el contenido completo
                    fh.write(contenido)
                for d in self._dirs_usuarios():
                    t_usr = d / f"{a['nombre']}.tmp"
                    if t_usr.exists():
                        os.replace(t_usr, d / a["nombre"])
                    else:
                        x = d / f".{a['nombre']}.lnk"
                        os.link(tmp, x)
                        os.replace(x, d / a["nombre"])
                os.replace(tmp, maestro / a["nombre"])
                cur.execute("UPDATE simulador.sftp_archivos SET estado='escrito', escrito_en=%s, bytes=%s, filas=%s, "
                            "sha256=%s WHERE id=%s",
                            (ahora, len(contenido), nfilas, hashlib.sha256(contenido).hexdigest(), a["id"]))
                self.contadores["sftp.archivos"] += 1
                cur.execute("SELECT COUNT(*) AS n FROM simulador.sftp_archivos WHERE ventana=%s AND estado<>'escrito'",
                            (a["ventana"],))
                if cur.fetchone()["n"] == 0:
                    cur.execute("DELETE FROM simulador.salida WHERE fuente='B' AND ventana=%s", (a["ventana"],))
            self.cn.commit()

    # ------------------------------------------------------------------ incidentes programados
    def _programados(self, ahora: float) -> None:
        horas = self.reloj.horas_simuladas(ahora)
        for i, inc in enumerate(self.cfg.get("programado", {}).get("incidentes", []) or []):
            if horas < float(inc["en_hora_simulada"]):
                continue
            clave = f"programado:{i}:{inc['tipo']}"
            if bd.leer_estado(self.cn, clave):
                continue
            params = dict(inc.get("parametros") or {})
            if "convivencia_horas_simuladas" in params:
                params["convivencia_min"] = float(params.pop("convivencia_horas_simuladas")) * 60 / self.reloj.factor
            nuevo = incidentes.disparar(self.cn, self.reloj, inc["tipo"], inc.get("minutos"), params, origen="programado")
            bd.escribir_estado(self.cn, clave, nuevo)
            self.cn.commit()
            self.incidentes = incidentes.activos(self.cn, ahora)
            log.info("incidente programado disparado", extra={"datos": {"tipo": inc["tipo"], "id": nuevo}})

    # ------------------------------------------------------------------ mantenimiento
    def _mantenimiento(self, ahora: float) -> None:
        self.ultimo_mant = ahora
        locales = [x.strip() for x in os.environ.get("SFTP_USUARIOS", "").split(",") if x.strip()]
        with self.cn.cursor() as cur:
            cur.execute("SELECT nombre FROM simulador.equipos ORDER BY nombre")
            equipos = [f["nombre"] for f in cur.fetchall()]
            limite = ahora - 3600 * float(self.cfg["retencion"]["api_horas"])
            while True:
                n = cur.execute("DELETE FROM simulador.api_eventos WHERE publicado_en < %s LIMIT 20000", (limite,))
                self.cn.commit()
                if n < 20000:
                    break
            cur.execute("DELETE FROM simulador.sftp_archivos WHERE estado='escrito' AND escrito_en < %s",
                        (ahora - 86400 * (float(self.cfg["retencion"]["sftp_dias"]) + 1),))
        self.cn.commit()
        self.usuarios_sftp = sorted(set(locales + equipos))
        # Retención SFTP y sincronización de equipos nuevos (enlaces duros al maestro).
        maestro = self.sftp_dir / "_maestro"
        maestro.mkdir(parents=True, exist_ok=True)
        limite_sftp = ahora - 86400 * float(self.cfg["retencion"]["sftp_dias"])
        dirs = self._dirs_usuarios()
        for f in maestro.iterdir():
            if f.name.startswith(".") or f.name.endswith(".tmp"):
                if f.stat().st_mtime < ahora - 3600:
                    f.unlink(missing_ok=True)
                continue
            if f.stat().st_mtime < limite_sftp:
                for d in dirs:
                    (d / f.name).unlink(missing_ok=True)
                f.unlink(missing_ok=True)
                continue
            for d in dirs:
                if not (d / f.name).exists():
                    x = d / f".{f.name}.lnk"
                    x.unlink(missing_ok=True)
                    os.link(f, x)
                    os.replace(x, d / f.name)


def main() -> None:
    configurar_logs()
    Generador().correr()


if __name__ == "__main__":
    main()
