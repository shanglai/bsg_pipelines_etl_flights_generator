# Reporte del simulador

Simulador de datos del proyecto final de **Pipelines de Datos y ETL con Python** (BSG Institute). Tres aerolíneas
ficticias publican por tres canales (API, SFTP, MySQL); el simulador guarda la verdad canónica y califica la
completitud de las entregas. Validado de punta a punta el 27 y 28 de octubre de 2026 en Linux (Docker 29.9,
Compose v5.6).

## 1. Estructura del repo

```
compose.yaml / compose.servidor.yaml   modo local / sobreposición del modo servidor
Dockerfile                             imagen única (generador, API, CLI `sim`), Python 3.11
sftp/                                  atmoz/sftp:alpine + llaves de host persistentes
config/simulador.yaml                  semilla, reloj, latencias, retención, frecuencias de error, programados
config/equipos.yaml                    equipos del modo servidor
datos/                                 aeropuertos.csv (40 reales), aerolineas.yaml (3 ficticias), rutas.csv (250 vuelos/día)
src/sim_aerolineas/                    plan, generador, formatos, api, incidentes, verdad, calificador, equipos, cli
tests/                                 19 pruebas pytest
validacion/                            scripts de la validación (consumidor mínimo, monitor, incidentes, frecuencias, carga)
docs/fuentes.md, docs/esquema_entrega.md   para alumnos
docs/instructor.md                     errores, incidentes, verdad, calificador, modo servidor
despliegue/README.md                   VM e2-small en GCP (solo documentación) y costo
```

Arquitectura en una línea: el **plan** de cada vuelo es una función pura de (semilla, llave del vuelo); el
**generador** mete el plan a una agenda en MySQL cuando el vuelo "nace", aplica a la verdad lo que ya ocurrió según el
reloj simulado y lo encola para su fuente; tres **publicadores** lo sacan con la latencia y los errores de cada canal.

## 2. Cómo levantar cada modo

Local (alumno):

```bash
cp .env.example .env      # cambiar contraseñas, llave y token
docker compose up -d --build
curl http://localhost:8000/salud
```

Servidor (instructor):

```bash
C="docker compose -f compose.yaml -f compose.servidor.yaml"
$C up -d mysql generador api
$C exec generador sim equipos aplicar
$C up -d sftp
```

| | Local | Servidor |
|---|---|---|
| Reloj | 60x, inicio fijo 2026-11-02, 48 h de precarga | 1x, inicio "ahora", 48 h de precarga |
| Reloj con el simulador apagado | se congela | sigue (es la hora real) |
| Credenciales | una de ejemplo, desde `.env` | por equipo, desde `config/equipos.yaml` |
| MySQL | sin TLS, puerto 3307 | `require_secure_transport=ON`, puerto 3306 |
| Puertos | 8000, 2222, 3307 (configurables) | 8000, 2222, 3306 |

## 3. Esquema publicado por cada fuente

Detalle completo en `docs/fuentes.md`. Ejemplos tomados de las corridas de validación:

**API (CQ, Aerolíneas Colibrí)**: `GET /v1/eventos?cursor=&limite=` con `X-API-Key`, paginación por cursor opaco en
orden de publicación.

```json
{"id_evento":"CQ-6d553da69782ace9","version_esquema":1,"tipo":"vuelo_creado","ocurrido_en":"2026-11-01T18:16:03-06:00",
 "aerolinea":"CQ","numero_vuelo":"CQ102","fecha_salida_local":"2026-11-08","origen":"CUN","destino":"MEX",
 "salida_programada_local":"2026-11-08T14:00:00","salida_programada_utc":"2026-11-08T19:00:00Z",
 "llegada_programada_local":"2026-11-08T15:10:00","llegada_programada_utc":"2026-11-08T21:10:00Z"}
{"id_evento":"CQ-6480f5fdf018b860","version_esquema":2,"tipo":"asiento_asignado","ocurrido_en":"2026-11-02T12:45:26-06:00",
 "aerolinea":"CQ","numero_vuelo":"CQ171","fecha_salida_local":"2026-11-03","asiento":"13B",
 "id_pasajero":"b01260603db2c891","canal_venta":"app"}
```

**SFTP (JG, Jaguar Air)**: `salida/JG_vuelos_<lote>_<fin ventana>.csv` y `salida/JG_asientos_...csv` cada 10 min.

```
numero_vuelo,fecha_salida,hora_salida,origen,ciudad_origen,destino,ciudad_destino,fecha_llegada,hora_llegada,estado,actualizado
JG251,06/12/2026,18:40,MTY,Monterrey,PVR,Puerto Vallarta,06/12/2026,20:15,PROGRAMADO,30/11/2026 22:18:07

id_movimiento,numero_vuelo,fecha_salida,asiento,pasajero,movimiento,clase,fecha_hora_movimiento
JG-35c611b9892ec09f,JG114,01/12/2026,23F,c5a2fe765ef91eb9,ASIGNACION,Y,30/11/2026 21:27:32
```

**MySQL (XJ, Aerovías Ajolote)**: base `aerolinea_c`, tablas `vuelos` y `asignaciones`.

```
id    numero_vuelo fecha_salida origen destino salida_local        llegada_local       estado  creado_en           actualizado_en
3358  XJ242        2026-12-10   KDFW   MMTJ    2026-12-10 10:05:00 2026-12-10 11:00:00 ABIERTO 2026-12-02 20:29:58 2026-12-02 20:29:58

id     vuelo_id asiento pasajero         asignado_en         actualizado_en
432847 3022     12B     d0835f1108011e02 2026-12-02 21:09:53 2026-12-02 21:09:53
```

## 4. Catálogo de errores e incidentes

Frecuencias por omisión (todas en `config/simulador.yaml`; `SIM_ERRORES=limpio` las pone en cero):

| Fuente | Error | Por omisión |
|---|---|---|
| API | 5xx intermitente | 2 % por petición |
| API | 429 + Retry-After | más de 60 peticiones/min por llave |
| API | Duplicado (otra página) | 1 % por evento |
| API | Llega 2 a 5 min tarde (fuera de orden) | 3 % por evento |
| API | Offset `-06:00` en vez de `Z` | 30 % por evento |
| API | Esquema v1 -> v2 (`pasajero`->`id_pasajero`, `canal_venta`) | programado a la hora simulada 96, 12 h de convivencia |
| SFTP | Truncado con nombre final (reenvío completo después) | 3 % por archivo |
| SFTP | Reenvío con el mismo nombre y contenido corregido | 5 % por archivo |
| SFTP | Windows-1252 / separador `;` | 15 % / 15 % por archivo |
| SFTP | Ventana 30 a 60 min tarde | 5 % por ventana |
| SFTP | `.tmp` a medio escribir visible 3 a 15 s | siempre |
| SFTP | Columna `clase` nueva sin aviso | desde la hora simulada 72 |
| MySQL | Transacción larga (3 a 5 min, `actualizado_en` viejo) | 8 % por lote |
| MySQL | Corrección manual sin mover `actualizado_en` | 3 % por cierre |
| MySQL | Borrado físico, ICAO, DATETIME local sin zona | siempre |
| Negocio | Dos pasajeros en un asiento / asiento inexistente / asignación en vuelo no abierto | 3 % / 2 % / 3 % por vuelo |
| Negocio | Origen igual a destino / ruta fuera de la red | 0.5 % / 0.5 % por vuelo |

Incidentes (CLI `sim incidente disparar <tipo>` o `POST /admin/incidentes` con `X-Token-Instructor`):
`caida_api`, `rafaga_duplicados`, `cambio_esquema_api`, `sftp_codificacion`, `pausa_c`, `reanudar_c`. Todos quedan
en `simulador.incidentes`. Detalle y ejemplos en `docs/instructor.md`.

## 5. Esquema de entrega que espera el calificador

DuckDB (`entrega_vuelos`, `entrega_asientos`) o carpeta con los dos Parquet. Ver `docs/esquema_entrega.md`.

- `entrega_vuelos(aerolinea, numero_vuelo, fecha_salida_local, origen, destino, salida_programada_utc, estado)`
- `entrega_asientos(aerolinea, numero_vuelo, fecha_salida_local, asiento, pasajero)`

`sim calificar --entrega <ruta> [--corte ISO] [--margen-min 15] [--verdad <export>] [--detalle <dir>]` reporta por
aerolínea: vuelos faltantes, sobrantes, con estado distinto y con atributos distintos; asientos faltantes, sobrantes y
en conflicto (solo vuelos cerrados o cancelados antes de corte - margen), y la completitud.

## 6. Resultados de la validación (números reales)

Se corrieron cuatro instancias: **errores** (60x, 30 min reales), **limpio** (360x, 33 min), **volumen** (1440x,
36 min = 36 días simulados, con ventanas SFTP de 30 s y lotes MySQL de 10 a 30 s para tener muestra) y **servidor**
(1x, 15 equipos).

### 6.1 Completitud

| Prueba | Vuelos | Asientos al cierre |
|---|---|---|
| Consumidor mínimo, modo **limpio** | **100.00 %** (1,796 de 1,796; 0 sobrantes) | **100.00 %** (86,053 de 86,053 en 685 vuelos) |
| El mismo consumidor, modo **con errores** | 79.37 % | 73.54 % |

En modo limpio el 100 % se mantuvo aunque el contenedor se reinició a mitad de la corrida (el generador procesó
16,180 eventos de atraso al volver). En modo con errores el calificador detectó exactamente lo que el consumidor
ingenuo no maneja:

| Aerolínea | Vuelos | Asientos | Causa |
|---|---|---|---|
| CQ (API) | 100 %, 4 sobrantes | 88.87 %, 218 en conflicto | vuelos inyectados (9xxx/8xxx); v2 renombró `pasajero` |
| JG (SFTP) | 0 % (137 faltantes) | 0 % | archivos en cp1252 con `;`, UTF-16 por incidente, ventana tarde |
| XJ (MySQL) | 100 %, 6 sobrantes | 100 % | vuelos inyectados; los asientos dobles cayeron fuera de los 19 vuelos evaluados |

La calificación contra la verdad exportada a Parquet dio los mismos números que contra MySQL.

### 6.2 Cada fuente entrega lo esperado

- Bitácora vs. plan recalculado: los errores de negocio registrados coinciden **exactamente** con los que el plan
  determinista predice (corrida de volumen: 266 / 164 / 234 / 57 / 51 sobre 9,547 vuelos).
- API: 429 con `Retry-After` (51 de 80 peticiones en ráfaga); 7 liberaciones llegaron antes de su asignación;
  401 con llave inválida. El 410 por cursor expirado está implementado pero no se ejercitó (requiere 72 h reales).
- SFTP: el monitor vio 4 archivos `.tmp`; aparecieron cp1252, `;` y UTF-16; la columna `clase` apareció después de
  la hora 72 (66 de 72 archivos de asientos en la corrida de volumen).
- MySQL: códigos ICAO válidos; 27 vuelos con origen = destino, 18 fuera de red, 167 asientos con dos pasajeros, 73
  asientos inexistentes, 4 asignaciones en vuelos cancelados; binlog ROW con solo tablas de `aerolinea_c`
  (ni `simulador` ni sentencias de usuarios).
- Trampa de la marca de agua: en la corrida con errores, **418 filas vigentes** de `asignaciones` habrían quedado
  fuera con una marca de agua sin traslape; el traslape mínimo necesario fue de 6.5 h simuladas (incluye la pausa de C).

### 6.3 Frecuencias observadas vs. configuradas

| Error | Config | Observado | Muestra |
|---|---|---|---|
| Asiento doble / inexistente / no abierto (por vuelo) | 3 % / 2 % / 3 % | 3.16 % / 1.81 % / 2.82 % | 9,547 vuelos |
| Origen = destino / fuera de red (por vuelo) | 0.5 % / 0.5 % | 0.60 % / 0.53 % | 9,547 vuelos |
| API duplicado | 1 % | 1.02 % | 541,025 eventos |
| API tarde | 3 % | 3.00 % | 541,025 eventos |
| API offset -06:00 | 30 % | 29.94 % | 541,025 eventos |
| API 5xx | 2 % | 2.3 % y 3.3 % | 1,209 y 300 peticiones |
| MySQL transacción larga | 8 % | 6.5 % | 107 lotes |
| MySQL corrección manual | 3 % | 3.16 % | 3,324 cierres |
| SFTP truncado / reenvío | 3 % / 5 % | 1.4 % / 9.0 % | 144 archivos |
| SFTP ventana tarde | 5 % | 11.1 % | 72 ventanas |
| SFTP cp1252 / `;` (bitácora) | 15 % / 15 % | 11.3 % / 16.7 % | 150 archivos |

Las de SFTP se desvían más porque la muestra es chica (72 ventanas en 36 minutos). Para descartar sesgo se evaluó
la función de azar sobre 200 mil nombres: 3.00 %, 5.00 %, 4.99 % y 14.90 %. No hay sesgo; es ruido de muestra.

### 6.4 Incidentes

| Incidente | Vía | Efecto verificado |
|---|---|---|
| `caida_api` 1 min | endpoint | 5 de 5 peticiones con 503; 200 al terminar |
| `rafaga_duplicados` 2 min, 0.5 | endpoint | 34.5 % de duplicados en lo publicado durante la ráfaga |
| `cambio_esquema_api` 2 min de convivencia | endpoint | 221 v1 + 190 v2 durante la convivencia; 118 de 118 en v2 después |
| `sftp_codificacion` utf-16, 2 archivos | CLI | los 2 archivos siguientes con BOM `FF FE` |
| `pausa_c` + `reanudar_c` | CLI | `MAX(id)` congelado 220 s; 1,085 filas confirmadas de golpe al reanudar |
| Todos | - | registrados en `simulador.incidentes` |

### 6.5 Modo servidor con 15 equipos

- Cada equipo leyó las tres fuentes con sus credenciales durante 6 min simultáneos: 55,155 eventos de API, 60
  descargas SFTP, 191 consultas MySQL.
- MySQL rechaza conexiones sin TLS (error 1045) y los equipos negocian `TLS_AES_256_GCM_SHA384`.
- Ningún usuario de equipo puede leer `simulador.*`; el chroot SFTP solo muestra `salida/`.
- Primera prueba: 233 errores de conexión en la API, por el keep-alive de 5 s de uvicorn frente a clientes que
  reutilizan la conexión. Se subió a 65 s; segunda prueba: 0 errores.

### 6.6 Recuperación y reloj congelado

- La API retiene 72 h y SFTP 7 días en tiempo real: una noche apagado (~12 h) cabe con holgura.
- Generador apagado 188 s en modo local: al volver, el reloj simulado siguió exactamente en 00:42:28 (sin congelar
  habría saltado ~3 h simuladas) y `/salud` reportó "degradado" durante el apagado.

## 7. Consumo de memoria y disco

| Escenario | MySQL | Generador | API | SFTP | Total |
|---|---|---|---|---|---|
| Servidor, 15 equipos leyendo (pico) | 272 MiB | 38 MiB | 60 MiB | 168 MiB | **~530 MiB** |
| Local tras 11 días simulados | 461 MiB | 75 MiB | 47 MiB | 4 MiB | ~590 MiB |
| Volumen 1440x, 36 días simulados | 462 MiB | 55 MiB | 43 MiB | 3 MiB | ~560 MiB |

MySQL tope por configuración: buffer pool de 256 MiB, ~460 MiB en régimen. Estimación para la e2-small con 15
equipos: **~0.8 GB** con el pool lleno y el SFTP con sesiones abiertas, más ~0.2 GB del sistema. Cabe en 2 GB.

Disco tras 36 días simulados (corrida de volumen):

| Concepto | Tamaño |
|---|---|
| Tablas de `simulador` + `aerolinea_c` | 763 MB (`verdad_eventos` 368, `api_eventos` 183, `verdad_asientos` 109, `asignaciones` 51) |
| Lo mismo en modo servidor (API retiene solo 72 h reales) | ~600 MB estimado |
| Directorio de datos de MySQL completo | 1.2 GB (incluye 128 MB de redo y 87 MB de binlog) |
| Archivos SFTP | 23 MB (enlaces duros: no se multiplica por equipo) |

La base queda por debajo de 2 GB en 5 semanas. Con 30 GB de disco sobra.

**Cuidado en modo local**: a 60x se generan ~2.3 millones de eventos por día real encendido (~600 MB). Si el
simulador se deja corriendo varios días sin parar, la base crece mucho. Con `docker compose stop` el reloj se congela
y no crece.

## 8. Costo estimado en GCP (aproximado)

Detalle en `despliegue/README.md`. Precios de lista de `us-central1`; verifícalos en la calculadora antes de presupuestar.

| | 2 semanas | 5 semanas |
|---|---|---|
| e2-small 24/7 | ~5.6 USD | ~14.1 USD |
| Disco pd-balanced 30 GB | ~1.4 USD | ~3.5 USD |
| IP fija | ~1.7 USD | ~4.2 USD |
| Salida a internet | ~0.5 a 1 USD | ~1.2 a 2.4 USD |
| Instantáneas | < 0.5 USD | < 1 USD |
| **Total** | **~9 a 10 USD** | **~24 a 25 USD** |

No se creó ningún recurso en la nube.

## 9. Decisiones y por qué

1. **Plan por vuelo como función pura de (semilla, llave)** en lugar de un generador aleatorio global. Así el mundo
   es reproducible aunque el generador se reinicie o vaya atrasado, y la verificación puede recalcular los errores
   esperados y compararlos uno por uno con la bitácora.
2. **La verdad y las colas viven en MySQL** (esquema `simulador`), no en memoria. Un reinicio no pierde nada (se
   comprobó con la caída real del contenedor). Las escrituras internas se excluyen del binlog con dos candados
   (`--binlog-ignore-db` y `sql_log_bin=0`), para que el CDC de los alumnos no exponga la verdad.
3. **Los errores de negocio son registros extra**: la verdad nunca se ensucia. Además se diseñaron para tener
   solución: el asiento doble cae sobre un asiento que el pasajero real conserva (gana el `asignado_en` menor), y
   todo archivo truncado o incompleto se reenvía completo. Sin esto, el 100 % no sería alcanzable.
4. **Una transacción larga retiene también los cambios posteriores de esos vuelos**, como un bloqueo real. Si no,
   MySQL quedaría inconsistente (una liberación aplicada antes que su asignación).
5. **SFTP con enlaces duros**: un archivo maestro y un enlace por equipo dentro del mismo volumen. Todos ven los
   mismos bytes sin multiplicar el disco, y un equipo nuevo recibe la historia retenida.
6. **Precarga de 48 h y reloj congelado al apagar**: sin la precarga, la primera hora de clase tenía 13 vuelos y
   ningún cierre; sin el congelamiento, prender la laptop en la mañana generaría 30 días simulados de golpe.
7. **El modo limpio apaga también los cambios programados** (esquema v2 y columna nueva), porque también son errores.
8. **Opciones de MySQL por línea de comandos**, no por `my.cnf` montado: en Windows un archivo montado queda con
   permisos de escritura para todos y MySQL lo ignora.
9. **Margen del calificador en minutos reales**: la latencia es real, no simulada; el calificador la convierte con el
   factor del reloj.

## 10. Pendientes y riesgos conocidos

- **No se probó en Windows ni en macOS**: solo en Linux. Se mitigaron los problemas típicos (finales de línea con
  `.gitattributes`, sin `my.cnf` montado, volúmenes con nombre para los enlaces duros, script del SFTP copiado
  dentro de la imagen), pero hay que probar en Docker Desktop antes de entregarlo.
- **Docker Hub limita descargas** (aquí respondió 429): `.env.example` deja cambiar las imágenes a `mirror.gcr.io`.
- **En modo local el alumno tiene la contraseña root** y puede leer la verdad. La calificación oficial debe usar la
  verdad del instructor. Como el mundo depende solo de la semilla y del reloj simulado, se puede calificar una entrega
  local contra el simulador del instructor si este avanzó al menos hasta el corte.
- **TLS de MySQL con certificado autofirmado**: los alumnos cifran pero no verifican identidad. Pendiente: publicar
  el `ca.pem` del servidor para que verifiquen.
- **El calificador necesita el corte**: los equipos deben reportar la hora simulada de su extracción (`/salud`).
- **SFTP en modo servidor** no arranca hasta que existan usuarios (`sim equipos aplicar`), y agregar equipos exige
  recrear el contenedor.
- **Una sola instancia de uvicorn**: probada con 15 equipos; con más de 30 conviene medir de nuevo.
- **Lo que depende del tiempo real no es reproducible** (en qué lote o página cae cada cambio). El contenido sí.
- **Windows-1252 solo se nota en los archivos de vuelos**: los de asientos son ASCII puro.
- **Las frecuencias de SFTP necesitan muchas ventanas** para verse en su valor; en una clase de 2 horas habrá pocos
  truncados o reenvíos. Si se quiere garantizar que aparezcan, conviene dispararlos o subir la frecuencia para esa
  sesión.
- El repo quedó con commits locales en la rama `claude/happy-dirac-6uu9ff`, **sin push**.
