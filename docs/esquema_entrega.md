# Esquema de entrega para el calificador de completitud

El calificador compara tu resultado contra la verdad del simulador. Entrega **una** de estas dos formas:

- Una base DuckDB (`equipo.duckdb`) con las tablas `entrega_vuelos` y `entrega_asientos`.
- Una carpeta con `entrega_vuelos.parquet` y `entrega_asientos.parquet`.

## entrega_vuelos

Un renglón por vuelo de las tres aerolíneas, con su estado más reciente.

| Columna | Tipo | Ejemplo | Regla |
|---|---|---|---|
| aerolinea | VARCHAR | `XJ` | código de dos letras |
| numero_vuelo | VARCHAR | `XJ123` | código + número sin ceros a la izquierda |
| fecha_salida_local | DATE | `2026-11-05` | fecha local de salida en el origen |
| origen | VARCHAR | `TIJ` | IATA |
| destino | VARCHAR | `GDL` | IATA |
| salida_programada_utc | TIMESTAMP | `2026-11-05 15:35:00` | UTC, sin zona |
| estado | VARCHAR | `programado` | `programado`, `cancelado` o `cerrado` |

Llave: (`aerolinea`, `numero_vuelo`, `fecha_salida_local`). Sin duplicados.

## entrega_asientos

Asientos vigentes por vuelo. Para un vuelo cerrado son los asientos **al cierre**; un vuelo cancelado no tiene asientos.

| Columna | Tipo | Ejemplo |
|---|---|---|
| aerolinea | VARCHAR | `CQ` |
| numero_vuelo | VARCHAR | `CQ102` |
| fecha_salida_local | DATE | `2026-11-08` |
| asiento | VARCHAR | `12C` |
| pasajero | VARCHAR | `9f2c41d07a6be3a1` |

Llave: (`aerolinea`, `numero_vuelo`, `fecha_salida_local`, `asiento`).

## Cómo se califica

- **Corte**: el momento simulado contra el que se compara (normalmente, cuando tu pipeline terminó de correr).
- **Margen**: los datos tardan en llegar. Por omisión 15 minutos reales (convertidos con el factor del reloj).
  No se exigen vuelos creados dentro del margen, y un estado se acepta si coincide con el de cualquier momento
  dentro del margen.
- **Vuelos**: faltantes, sobrantes (vuelos que no existen en la verdad, por ejemplo los que debiste mandar a
  cuarentena), estado distinto y atributos distintos (origen, destino, salida UTC).
- **Asientos**: solo de vuelos cerrados o cancelados antes de (corte - margen), porque ya son definitivos.
  Faltantes, sobrantes (incluye asientos en vuelos cancelados o asientos inexistentes) y en conflicto (mismo asiento,
  otro pasajero).
- **Completitud** = correctos / esperados, por aerolínea y en total.

El reporte se ve así:

```
VUELOS
aerolínea  esperados correctos faltantes sobrantes  estado≠  atrib≠   dup  margen   completitud
CQ               412       405         2         3        5       0     0      21      98.30 %
...
ASIENTOS AL CIERRE (vuelos cerrados o cancelados antes de corte - margen)
aerolínea   vuelos esperados correctos faltantes sobrantes conflicto   dup   completitud
CQ              96     12650     12590        60        14        11     0      99.53 %
```
