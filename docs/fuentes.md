# Contrato de las fuentes

Documento para alumnos. Describe lo que publica cada aerolínea, cómo se accede y las reglas de
negocio. Las tres aerolíneas son **ficticias**: los nombres y los códigos de dos letras se
inventaron para el curso.

| Aerolínea | Código | Canal | Hubs |
|---|---|---|---|
| Aerolíneas Colibrí | CQ | API REST | MEX, CUN |
| Jaguar Air | JG | Archivos por SFTP | MTY |
| Aerovías Ajolote | XJ | Base de datos MySQL 8.4 (solo lectura) | TIJ, GDL |

Catálogos de referencia (versionados en el repo, también se entregan a los alumnos):

- `datos/aeropuertos.csv`: 40 aeropuertos reales con IATA, ICAO, ciudad, país, zona horaria IANA, latitud y longitud.
- `datos/rutas.csv`: la red publicada de cada aerolínea (número de vuelo, origen, destino, hora local de salida).
  Un vuelo cuyo par origen-destino no aparece aquí para su aerolínea **no es válido**.

## Modelo de negocio

- Todos los vuelos tienen el mismo mapa de asientos: filas 1 a 30, letras A a F (180 asientos).
- **Llave de negocio de un vuelo**: (aerolínea, número de vuelo, fecha de salida local en el aeropuerto de origen).
  Los números se repiten cada día.
- Ciclo de vida: `creado` (entre 1 y 14 días antes de salir) -> `cerrado` (1 hora antes de la salida; ya no admite
  cambios) o `cancelado` (antes de cerrar).
- Asientos: se asignan y se liberan mientras el vuelo está abierto. Un pasajero que cambia de asiento libera uno y
  toma otro.
- Pasajeros: solo un identificador opaco (hash). No hay datos personales.

### Reglas de negocio (lo que debe cumplir un dato válido)

1. Un asiento tiene a lo más un pasajero. Si llega una asignación sobre un asiento ocupado, la asignación nueva es
   inválida: se queda el pasajero que ya lo tenía.
2. El asiento existe en el mapa (1A a 30F).
3. No hay asignaciones en un vuelo cerrado o cancelado. Cancelar un vuelo libera todos sus asientos
   (las fuentes NO publican una liberación por asiento al cancelar).
4. Origen distinto de destino.
5. La ruta (origen, destino) pertenece a la red publicada de esa aerolínea.

Las fuentes pueden traer datos que rompen estas reglas. Tu pipeline los detecta, los manda a cuarentena y no los
deja pasar a silver.

## Latencia y retención

| Fuente | Cuándo aparece un cambio (tiempo real) | Retención |
|---|---|---|
| API (CQ) | Segundos después de ocurrir; a veces minutos (fuera de orden) | 72 horas |
| SFTP (JG) | Un par de archivos cada 10 minutos; a veces tarde o reenviados | 7 días |
| MySQL (XJ) | En lotes de 30 a 90 segundos; a veces una transacción tarda varios minutos | Estado completo, siempre |

Todas las marcas de tiempo de los datos salen de un **reloj simulado**. En modo local corre acelerado
(1 minuto real = 1 hora simulada); en el servidor del curso corre a velocidad real. La latencia, en cambio, se
mide en tiempo real. Consulta la hora simulada en `GET /salud`.

---

## Aerolínea CQ: API REST

- Base: `http://localhost:8000` en modo local (en el servidor, la URL que te dé tu instructor).
- Autenticación: encabezado `X-API-Key: <tu llave>`.
- Límite: 60 peticiones por minuto por llave. Al excederlo responde `429` con el encabezado `Retry-After`
  (segundos). Puede responder errores `5xx` de vez en cuando: reintenta con espera.

### GET /v1/eventos

| Parámetro | Descripción |
|---|---|
| `cursor` | Opaco. Omítelo la primera vez (empiezas en el evento más antiguo retenido). Después usa `siguiente_cursor`. |
| `limite` | 1 a 500 (por omisión 100). |

Respuesta:

```json
{
  "datos": [ { ...evento... } ],
  "siguiente_cursor": "czo0NTMy",
  "hay_mas": true
}
```

- El cursor sigue el **orden de publicación**, no el de ocurrencia. Guarda el cursor (checkpoint) para reanudar.
- Si `hay_mas` es `false`, ya leíste todo lo publicado hasta ahora; vuelve a preguntar más tarde con el mismo cursor.
- Un cursor que apunta a eventos con más de 72 horas responde `410`.
- El mismo evento (`id_evento`) puede aparecer más de una vez.

### Eventos

Campos comunes: `id_evento`, `version_esquema`, `tipo`, `ocurrido_en` (ISO 8601 con zona: `Z` u offset),
`aerolinea`, `numero_vuelo`, `fecha_salida_local` (AAAA-MM-DD).

`vuelo_creado`

```json
{"id_evento": "CQ-6d553da69782ace9", "version_esquema": 1, "tipo": "vuelo_creado",
 "ocurrido_en": "2026-11-01T18:16:03-06:00", "aerolinea": "CQ", "numero_vuelo": "CQ102",
 "fecha_salida_local": "2026-11-08", "origen": "CUN", "destino": "MEX",
 "salida_programada_local": "2026-11-08T14:00:00", "salida_programada_utc": "2026-11-08T19:00:00Z",
 "llegada_programada_local": "2026-11-08T15:10:00", "llegada_programada_utc": "2026-11-08T21:10:00Z"}
```

`vuelo_cancelado`, `vuelo_cerrado`: solo los campos comunes.

`asiento_asignado`, `asiento_liberado`:

```json
{"id_evento": "CQ-0b1f...", "version_esquema": 1, "tipo": "asiento_asignado",
 "ocurrido_en": "2026-11-03T14:22:05Z", "aerolinea": "CQ", "numero_vuelo": "CQ102",
 "fecha_salida_local": "2026-11-08", "asiento": "12C", "pasajero": "9f2c41d07a6be3a1"}
```

Las horas `*_local` no tienen zona: son la hora del aeropuerto correspondiente (origen para la salida, destino para
la llegada).

**Versión 2 del esquema.** En algún momento la API empieza a publicar `version_esquema: 2`. Durante un periodo
conviven ambas versiones. En v2 el campo `pasajero` se llama `id_pasajero` y las asignaciones traen un campo nuevo,
`canal_venta` (`web`, `app`, `agencia`, `mostrador`).

### GET /salud

Sin autenticación. Regresa el estado del simulador y la hora simulada (`reloj_simulado`).

---

## Aerolínea JG: archivos por SFTP

- Servidor: `localhost`, puerto `2222` en modo local. Usuario y contraseña de tu `.env` (o los que te dé tu instructor).
- Carpeta: `salida/` (solo lectura).
- Cada 10 minutos reales se publican dos archivos de la misma ventana:
  - `JG_vuelos_<lote>_<AAAAMMDDTHHMM>.csv`: foto de los vuelos que cambiaron en la ventana (su último estado).
  - `JG_asientos_<lote>_<AAAAMMDDTHHMM>.csv`: movimientos de asientos de la ventana.
  - `<lote>` es un consecutivo de seis dígitos; `<AAAAMMDDTHHMM>` es el fin de la ventana en hora simulada UTC.
- El proveedor escribe primero `<nombre>.csv.tmp` y lo renombra al terminar. Un archivo `.tmp` está incompleto.
- Un archivo puede volver a publicarse **con el mismo nombre** y contenido corregido.
- Codificación normal: UTF-8, separador coma, primera fila con encabezados. Algunos archivos llegan de un sistema
  heredado (otra codificación u otro separador). No hay aviso previo.

`JG_vuelos_*.csv`

| Columna | Ejemplo | Nota |
|---|---|---|
| numero_vuelo | JG104 | |
| fecha_salida | 05/11/2026 | dd/mm/aaaa, local del origen |
| hora_salida | 07:15 | local del origen |
| origen, ciudad_origen | MTY, Monterrey | IATA |
| destino, ciudad_destino | GDL, Guadalajara | IATA |
| fecha_llegada, hora_llegada | 05/11/2026, 08:50 | local del destino |
| estado | PROGRAMADO, CANCELADO, CERRADO | |
| actualizado | 03/11/2026 22:41:09 | local del origen, sin zona |

`JG_asientos_*.csv`

| Columna | Ejemplo | Nota |
|---|---|---|
| id_movimiento | JG-5e1a90c7d2b44f10 | único por movimiento |
| numero_vuelo | JG104 | |
| fecha_salida | 05/11/2026 | dd/mm/aaaa |
| asiento | 17D | |
| pasajero | 3c9e0a71f4d2b886 | |
| movimiento | ASIGNACION, LIBERACION | |
| fecha_hora_movimiento | 04/11/2026 09:12:55 | local del aeropuerto de origen del vuelo, sin zona |

---

## Aerolínea XJ: MySQL 8.4

- Servidor: `127.0.0.1`, puerto `3307` en modo local; base `aerolinea_c`; usuario de solo lectura.
  En el servidor del curso la conexión exige TLS.
- El binlog está habilitado en formato ROW y tu usuario tiene privilegios de replicación: puedes hacer CDC.

`vuelos`

| Columna | Tipo | Nota |
|---|---|---|
| id | INT | llave técnica |
| numero_vuelo | VARCHAR(8) | XJ123 |
| fecha_salida | DATE | local del origen |
| origen, destino | CHAR(4) | **código ICAO** (MMTJ, MMGL, KLAX...) |
| salida_local | DATETIME | hora local del origen, sin zona |
| llegada_local | DATETIME | hora local del destino, sin zona |
| estado | VARCHAR(12) | ABIERTO, CANCELADO, CERRADO |
| creado_en, actualizado_en | DATETIME | UTC |

`asignaciones`

| Columna | Tipo | Nota |
|---|---|---|
| id | BIGINT | llave técnica |
| vuelo_id | INT | `vuelos.id` |
| asiento | VARCHAR(4) | |
| pasajero | VARCHAR(16) | |
| asignado_en, actualizado_en | DATETIME | UTC |

- Liberar un asiento **borra la fila**. Cancelar un vuelo borra todas sus asignaciones.
- No hay llaves foráneas ni restricciones de unicidad (sistema heredado).
- `actualizado_en` se mueve con cada cambio... casi siempre.

---

## Qué se entrega

Ver [esquema_entrega.md](esquema_entrega.md).
