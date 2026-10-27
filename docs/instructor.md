# Guía del instructor

Este documento NO se distribuye a los alumnos: describe los errores inyectados, los incidentes, la verdad y el
calificador.

## Catálogo de errores (frecuencias por omisión)

Todo se configura en `config/simulador.yaml` (sección `errores`). Con `SIM_ERRORES=limpio` todas las frecuencias
quedan en cero (modo limpio, útil para probar consumidores). Todas las decisiones salen de la semilla 987654 y de una
llave estable (id de evento, nombre de archivo, llave del vuelo), así que con la misma semilla y el mismo reloj se
inyectan exactamente los mismos errores.

| Fuente | Error | Por omisión | Unidad | Cómo se ve | Registro en la bitácora |
|---|---|---|---|---|---|
| API | 5xx intermitente | 2 % | petición | 500, 502 o 503 sin `Retry-After` | contador `api.5xx_inyectado` |
| API | 429 por límite | 60/min | llave | `Retry-After` en segundos | contador `api.429` |
| API | Duplicado | 1 % | evento | mismo `id_evento` publicado 1 a 40 s después (otra página) | `duplicado` |
| API | Llega tarde | 3 % | evento | 2 a 5 min de retraso: llega fuera de orden (p. ej., liberar antes de asignar) | `evento_tarde` |
| API | Offset mezclado | 30 % | evento | `ocurrido_en` con `-06:00` en vez de `Z` (mismo instante) | contador `api.offset_local` |
| API | Cambio de esquema v1 -> v2 | programado (hora simulada 96) | - | `pasajero` -> `id_pasajero`, nuevo `canal_venta`; convivencia 12 h simuladas | incidente `cambio_esquema_api` |
| SFTP | Archivo truncado | 3 % | archivo | queda con su nombre final cortado a la mitad de una línea; la versión completa llega 10 a 40 min después con el mismo nombre | `archivo_truncado` |
| SFTP | Reenvío corregido | 5 % | archivo | primera versión sin 1 a 3 filas; la corregida llega 10 a 40 min después con el mismo nombre | `reenvio_corregido` |
| SFTP | Windows-1252 | 15 % | archivo | acentos (Mérida, São Paulo) ilegibles como UTF-8; fin de línea CRLF | `formato_distinto` |
| SFTP | Punto y coma | 15 % | archivo | separador `;` | `formato_distinto` |
| SFTP | Archivo tarde | 5 % | ventana | los dos archivos llegan 30 a 60 min tarde | `archivo_tarde` |
| SFTP | Archivo temporal visible | siempre | archivo | `.csv.tmp` a medio escribir durante 3 a 15 s | - |
| SFTP | Columna nueva sin aviso | desde la hora simulada 72 | - | `clase` aparece en asientos, ANTES de la última columna | - |
| MySQL | Transacción larga | 8 % | lote | el 25 % de los vuelos del lote (y sus cambios posteriores) confirman 3 a 5 min tarde con `actualizado_en` viejo | `transaccion_larga` |
| MySQL | Corrección manual | 3 % | cierre de vuelo | `estado='CERRADO'` sin mover `actualizado_en` | `correccion_manual` |
| MySQL | Borrado físico | siempre | - | liberar o cancelar borra filas | - |
| MySQL | ICAO, hora local sin zona | siempre | - | por diseño | - |
| Negocio | Dos pasajeros en un asiento | 3 % | vuelo | asignación extra sobre un asiento que el pasajero real conserva hasta el cierre | `asiento_doble` |
| Negocio | Asiento inexistente | 2 % | vuelo | 31A, 31C, 32B, 12G, 20H, 0A, 15K | `asiento_inexistente` |
| Negocio | Asignación en vuelo cerrado o cancelado | 3 % | vuelo | 1 a 40 min simulados después del cierre o cancelación | `asignacion_vuelo_no_abierto` |
| Negocio | Origen igual a destino | 0.5 % | vuelo | vuelo extra con número 9xxx | `origen_igual_destino` |
| Negocio | Ruta fuera de la red | 0.5 % | vuelo | vuelo extra con número 8xxx a un aeropuerto fuera de la red | `ruta_fuera_de_red` |

Los errores de negocio se publican en la fuente de la aerolínea del vuelo (cualquiera de las tres), se registran en
`simulador.errores_inyectados` y **nunca** entran a la verdad. La verdad siempre está limpia.

Decisiones para que los errores tengan solución (y el 100 % sea alcanzable):

- `asiento_doble` elige un asiento que el pasajero real conserva hasta el cierre: así, en MySQL, la fila buena es
  la de `asignado_en` menor, aunque ambas sigan vivas.
- Un archivo truncado o con filas faltantes siempre se reenvía completo con el mismo nombre.
- Una transacción larga retiene también los cambios posteriores de esos vuelos (como lo haría un bloqueo real),
  para que el estado final de MySQL sea consistente.

## Incidentes

Se disparan por CLI o por el endpoint protegido. Todos quedan en `simulador.incidentes` (parte de la verdad).

| Tipo | Efecto | Parámetros |
|---|---|---|
| `caida_api` | La API responde 503 (con `Retry-After: 30`) a todo. Los eventos se acumulan y salen al volver. | `--minutos` (5) |
| `rafaga_duplicados` | Una proporción de los eventos de la API se publica dos veces. | `--minutos` (5), `proporcion` (0.3) |
| `cambio_esquema_api` | Pasa la API a v2, con convivencia. Permanente. | `convivencia_min` (30) |
| `sftp_codificacion` | Los siguientes N archivos salen en otra codificación. | `codificacion` (utf-16), `archivos` (2) |
| `pausa_c` | La aerolínea C deja de confirmar. | `--minutos` (sin fin: hasta `reanudar_c`) |
| `reanudar_c` | Termina la pausa: todo lo acumulado se confirma de golpe. | - |

CLI (dentro del contenedor):

```bash
docker compose exec generador sim incidente tipos
docker compose exec generador sim incidente disparar caida_api --minutos 10
docker compose exec generador sim incidente disparar rafaga_duplicados --minutos 5 --param proporcion=0.4
docker compose exec generador sim incidente disparar sftp_codificacion --param codificacion=utf-16 --param archivos=2
docker compose exec generador sim incidente disparar pausa_c
docker compose exec generador sim incidente disparar reanudar_c
docker compose exec generador sim incidente listar
docker compose exec generador sim incidente terminar 7
```

Endpoint (útil en el servidor, desde la laptop del instructor):

```bash
curl -X POST http://IP:8000/admin/incidentes -H "X-Token-Instructor: $TOKEN" \
     -H "Content-Type: application/json" -d '{"tipo": "caida_api", "minutos": 10}'
curl -X POST http://IP:8000/admin/incidentes/7/terminar -H "X-Token-Instructor: $TOKEN"
curl "http://IP:8000/admin/estado?token=$TOKEN"        # página de texto
```

También se pueden programar en `config/simulador.yaml` (`programado.incidentes`), en horas simuladas desde el inicio.

## La verdad

Esquema `simulador` en MySQL, sin acceso para alumnos y fuera del binlog (`--binlog-ignore-db=simulador` y
`sql_log_bin=0`):

- `verdad_eventos`: cada evento real con su momento simulado y real.
- `verdad_vuelos`: estado final de cada vuelo y sus momentos de creación, cancelación y cierre.
- `verdad_asientos`: asignaciones vigentes; para un vuelo cerrado son las asignaciones al cierre.
- `incidentes`, `errores_inyectados`, `contadores`.

Exportar hasta un momento dado:

```bash
docker compose exec generador sim verdad exportar --salida /entregas/verdad --hasta 2026-11-06T12:00:00Z
```

Escribe en `./entregas/verdad/`: `eventos`, `vuelos` (estado en ese momento), `asientos_vigentes`,
`asientos_al_cierre`, `incidentes`, `errores_inyectados` (Parquet) y `meta.json`.

**Propiedad útil**: el mundo depende solo de la semilla y del momento simulado. Dos simuladores con la misma semilla
e `inicio_simulado` generan los mismos vuelos, pasajeros y errores de negocio. Por eso puedes calificar una entrega
hecha en la laptop de un alumno contra la verdad de tu propio simulador, siempre que el tuyo haya avanzado al menos
hasta el corte.

## Calificador de completitud

```bash
# Copia la entrega del equipo a ./entregas/ (se monta en /entregas)
docker compose exec generador sim calificar --entrega /entregas/equipo01.duckdb
docker compose exec generador sim calificar --entrega /entregas/equipo01 --corte 2026-11-06T12:00:00Z \
    --margen-min 15 --detalle /entregas/detalle_equipo01
docker compose exec generador sim calificar --entrega /entregas/equipo01.duckdb --verdad /entregas/verdad --json
```

- `--corte`: hora simulada en que el equipo extrajo (pídeles que la reporten; está en `GET /salud`).
- `--margen-min`: minutos REALES de latencia tolerada. 15 cubre API y MySQL. Si quieres exigir también los archivos
  SFTP que llegan tarde, usa 70.
- `--detalle`: escribe `diferencias_vuelos.csv`, `diferencias_asientos.csv` y `resultado.json` para retroalimentar.

## Modo servidor

1. Edita `config/equipos.yaml` con los equipos.
2. `docker compose -f compose.yaml -f compose.servidor.yaml up -d`
3. `docker compose -f compose.yaml -f compose.servidor.yaml exec generador sim equipos aplicar`
4. `docker compose -f compose.yaml -f compose.servidor.yaml up -d --force-recreate sftp`
5. Copia las credenciales: `docker compose -f compose.yaml -f compose.servidor.yaml cp generador:/datos/secretos/credenciales_equipos.csv ./secretos/`
   (está en `.gitignore`; repártelas por un canal privado).

Cada equipo recibe un usuario MySQL de solo lectura con `REQUIRE SSL`, un usuario SFTP enjaulado con su propia
carpeta `salida/` (enlaces duros a los mismos archivos: todos ven los mismos datos) y una llave de API con su propio
límite. Agregar equipos después: edita el YAML y repite los pasos 3 y 4.

## Riesgos a tener presentes

- En modo local, el alumno tiene la contraseña root de su MySQL y podría leer `simulador.verdad_*`. Es aceptable para
  practicar; la calificación oficial se hace con la verdad del instructor.
- Reiniciar el generador no pierde datos (todo vive en MySQL), pero si está apagado, al volver procesa el atraso de
  golpe: la API publica todo con latencia mínima y SFTP junta el atraso en el siguiente archivo.
