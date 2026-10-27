# Simulador de aerolíneas para el proyecto final

Simulador de datos del proyecto final del curso **Pipelines de Datos y ETL con Python** (BSG Institute,
programa Python Certified Data Engineer). Publica la operación de tres aerolíneas **ficticias** por tres canales
distintos, con latencias, errores de datos e incidentes realistas, y guarda la verdad canónica para calificar la
completitud de los pipelines de los alumnos.

> Las aerolíneas Colibrí (CQ), Jaguar Air (JG) y Aerovías Ajolote (XJ) son inventadas. Sus códigos de dos letras
> también: si alguno coincide con una asignación real de IATA, es casualidad. Los 40 aeropuertos sí son reales.

| Aerolínea | Canal | Puerto local |
|---|---|---|
| CQ, Aerolíneas Colibrí | API REST (FastAPI) | 8000 |
| JG, Jaguar Air | Archivos CSV por SFTP | 2222 |
| XJ, Aerovías Ajolote | MySQL 8.4, solo lectura | 3307 |

## Levantar en modo local (alumno)

Requisitos: Docker Desktop (Windows o macOS) o Docker Engine con Compose v2 (Linux). ~1 GB de RAM libre.

```bash
cp .env.example .env          # en Windows PowerShell: Copy-Item .env.example .env
# edita .env: contraseñas, llave de API y token (no lo subas a Git)
docker compose up -d --build
docker compose ps
curl http://localhost:8000/salud
```

Si Docker Hub te responde `429 Too Many Requests` al descargar imágenes, cambia en `.env`:
`IMAGEN_MYSQL=mirror.gcr.io/library/mysql:8.4` e `IMAGEN_PYTHON=mirror.gcr.io/library/python:3.11-slim`.

Probar cada fuente:

```bash
# API
curl -H "X-API-Key: <ALUMNO_API_LLAVE>" "http://localhost:8000/v1/eventos?limite=5"
# SFTP (contraseña ALUMNO_SFTP_CONTRASENA)
sftp -P 2222 alumno@localhost      # luego: ls salida
# MySQL (contraseña ALUMNO_MYSQL_CONTRASENA)
mysql -h 127.0.0.1 -P 3307 -u alumno -p aerolinea_c -e "SELECT * FROM vuelos LIMIT 5"
```

En modo local el reloj corre a 60x (1 minuto real = 1 hora simulada) y arranca con 48 horas simuladas de historia,
así que desde el primer minuto hay vuelos programados, cerrados y cancelados. Apagar y volver a prender:
`docker compose stop` / `docker compose start` (no se pierde nada). Empezar de cero: `docker compose down -v`.

El contrato de cada fuente está en [docs/fuentes.md](docs/fuentes.md) y lo que se entrega al calificador en
[docs/esquema_entrega.md](docs/esquema_entrega.md).

## Modo servidor (instructor, varios equipos)

Misma imagen, otra sobreposición de Compose: reloj a velocidad real, MySQL con TLS obligatorio y credenciales por
equipo. Todos los equipos ven los mismos datos.

```bash
C="docker compose -f compose.yaml -f compose.servidor.yaml"
$C up -d mysql generador api
$C exec generador sim equipos aplicar        # lee config/equipos.yaml
$C up -d sftp
```

Detalles en [docs/instructor.md](docs/instructor.md) y despliegue en GCP en [despliegue/README.md](despliegue/README.md).

## Herramientas de instructor

```bash
docker compose exec generador sim estado                               # conteos, últimos eventos, incidentes
docker compose exec generador sim incidente disparar caida_api --minutos 10
docker compose exec generador sim verdad exportar --salida /entregas/verdad
docker compose exec generador sim calificar --entrega /entregas/equipo01.duckdb
```

`./entregas/` de tu máquina se monta en `/entregas` dentro del contenedor. Página de estado en el navegador:
`http://localhost:8000/admin/estado?token=<TOKEN_INSTRUCTOR>`.

## Estructura

```
.
├── compose.yaml                 modo local
├── compose.servidor.yaml        sobreposición del modo servidor
├── Dockerfile                   imagen única: generador, API y CLI `sim`
├── .env.example                 variables (secretos van en .env, fuera de Git)
├── config/
│   ├── simulador.yaml           semilla, reloj, latencias, retención, frecuencias de error, programados
│   └── equipos.yaml             equipos del modo servidor
├── datos/
│   ├── aeropuertos.csv          40 aeropuertos reales (IATA, ICAO, zona horaria, coordenadas)
│   ├── aerolineas.yaml          las 3 aerolíneas ficticias y sus redes
│   └── rutas.csv                vuelos diarios programados (generado con herramientas/generar_rutas.py)
├── src/sim_aerolineas/
│   ├── plan.py                  plan determinista de cada vuelo (verdad + errores de negocio)
│   ├── generador.py             bucle principal: nacer, agenda, verdad, publicación A/B/C
│   ├── formatos.py              cómo se ve cada evento en cada fuente
│   ├── api/app.py               API de la aerolínea A y endpoints de instructor
│   ├── incidentes.py            incidentes programables
│   ├── verdad.py                exportación de la verdad a Parquet
│   ├── calificador.py           calificador de completitud
│   ├── equipos.py               credenciales por equipo (modo servidor)
│   └── cli.py                   comando `sim`
├── sftp/                        atmoz/sftp con llaves de host persistentes
├── tests/                       pytest: catálogo, plan, reglas, errores, formatos, calificador
├── validacion/                  scripts de la validación de punta a punta (no son el pipeline de alumnos)
├── docs/                        fuentes (alumnos), esquema de entrega (alumnos), guía del instructor
└── despliegue/README.md         VM e2-small en GCP (solo documentación)
```

## Pruebas

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[pruebas]"
pytest -q
```

## Reproducibilidad

Todo lo aleatorio sale de la semilla `987654` y de una llave estable (llave del vuelo, id del evento, nombre del
archivo). El mundo simulado (vuelos, pasajeros, asientos, cancelaciones y errores de negocio) es función solo de la
semilla y del momento simulado: con la misma semilla y el mismo reloj, es idéntico. Lo que depende del tiempo real
(en qué lote de MySQL cae un cambio, en qué página de la API) puede variar entre corridas.
