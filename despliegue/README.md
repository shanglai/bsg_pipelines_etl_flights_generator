# Despliegue en GCP: una VM e2-small con Docker Compose

Guía para el instructor. **Este repositorio no crea recursos en la nube**: los comandos de abajo los ejecutas tú,
revisándolos antes, con tu proyecto y tu facturación.

## Arquitectura

```
alumnos ──► IP fija ──► firewall (8000 API, 2222 SFTP, 3306 MySQL con TLS)
                          │
                    VM e2-small (2 vCPU compartidas, 2 GB) · Debian 12 · disco 30 GB
                          │
                    docker compose (modo servidor)
                    ├─ mysql      MySQL 8.4 (TLS obligatorio, binlog ROW)
                    ├─ generador  reloj factor 1, verdad, publicación
                    ├─ api        FastAPI (A) + /salud + /admin
                    └─ sftp       atmoz/sftp (B), un usuario enjaulado por equipo
```

Consumo medido con 15 equipos: ver `REPORTE_SIMULADOR.md` (sección de consumo). Cabe en 2 GB con holgura; aun así
se recomienda 1 GB de swap como red de seguridad.

## 1. Preparar el proyecto

```bash
gcloud config set project TU_PROYECTO
gcloud config set compute/region us-central1
gcloud config set compute/zone us-central1-a
gcloud services enable compute.googleapis.com
```

Región: `us-central1` es de las más baratas. `northamerica-south1` (Querétaro) da menos latencia desde México pero
cuesta algo más; revisa la tabla de precios antes de elegir.

## 2. IP fija

```bash
gcloud compute addresses create sim-aerolineas-ip --region us-central1
gcloud compute addresses describe sim-aerolineas-ip --region us-central1 --format='value(address)'
```

## 3. Firewall (solo los puertos necesarios)

```bash
# Puertos del simulador para alumnos. Si conoces los rangos del campus, ponlos en --source-ranges.
gcloud compute firewall-rules create sim-aerolineas-alumnos \
  --network default --direction INGRESS --action ALLOW \
  --rules tcp:8000,tcp:2222,tcp:3306 --target-tags sim-aerolineas --source-ranges 0.0.0.0/0

# SSH de administración solo por IAP (sin exponer el 22 a internet).
gcloud compute firewall-rules create sim-aerolineas-ssh-iap \
  --network default --direction INGRESS --action ALLOW \
  --rules tcp:22 --target-tags sim-aerolineas --source-ranges 35.235.240.0/20
```

## 4. VM

```bash
gcloud compute instances create sim-aerolineas \
  --machine-type e2-small \
  --image-family debian-12 --image-project debian-cloud \
  --boot-disk-size 30GB --boot-disk-type pd-balanced \
  --address sim-aerolineas-ip --tags sim-aerolineas \
  --metadata enable-oslogin=TRUE
```

## 5. Instalar Docker y levantar el simulador

```bash
gcloud compute ssh sim-aerolineas --tunnel-through-iap

# En la VM:
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# Swap de 1 GB (red de seguridad en 2 GB de RAM)
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

git clone <URL_DEL_REPO> simulador && cd simulador
cp .env.example .env && nano .env      # contraseñas largas; TOKEN_INSTRUCTOR propio
nano config/equipos.yaml               # lista de equipos

C="docker compose -f compose.yaml -f compose.servidor.yaml"
$C build
$C up -d mysql generador api           # el SFTP necesita usuarios: va después
$C exec generador sim equipos aplicar
$C up -d sftp
$C cp generador:/datos/secretos/credenciales_equipos.csv ./credenciales_equipos.csv
curl -s localhost:8000/salud
```

Reparte a cada equipo su renglón de `credenciales_equipos.csv` por un canal privado y bórralo de la VM cuando termines.
El puerto 3306 exige TLS: los alumnos se conectan con `ssl={"ca": None}` / `ssl_mode=REQUIRED` según su cliente.

## 6. Respaldo

Opción A, instantáneas del disco (incluye todo):

```bash
gcloud compute resource-policies create snapshot-schedule sim-aerolineas-diario \
  --region us-central1 --max-retention-days 7 --daily-schedule --start-time 08:00
gcloud compute disks add-resource-policies sim-aerolineas \
  --resource-policies sim-aerolineas-diario --zone us-central1-a
```

Opción B, volcado lógico a Cloud Storage (más chico, solo la verdad y la fuente C):

```bash
docker compose exec -T mysql sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction \
  --databases simulador aerolinea_c' | gzip > respaldo_$(date +%F).sql.gz
gsutil cp respaldo_$(date +%F).sql.gz gs://TU_BUCKET/simulador/
```

Antes de cerrar el curso exporta la verdad final: `$C exec generador sim verdad exportar --salida /entregas/verdad_final`.

## 7. Apagado y limpieza

```bash
# Pausa (deja de cobrar CPU/RAM; el disco y la IP se siguen cobrando):
gcloud compute instances stop sim-aerolineas

# Fin del curso (borra todo; verifica antes de ejecutar):
gcloud compute instances delete sim-aerolineas
gcloud compute addresses delete sim-aerolineas-ip --region us-central1
gcloud compute firewall-rules delete sim-aerolineas-alumnos sim-aerolineas-ssh-iap
gcloud compute resource-policies delete sim-aerolineas-diario --region us-central1
```

Si detienes la VM a medio curso, el reloj simulado (factor 1) sigue avanzando; al volver, el generador procesa el
atraso de golpe. Es mejor no detenerla mientras haya equipos trabajando.

## 8. Costo estimado (APROXIMADO)

Precios de lista de referencia en `us-central1` (verifícalos en la calculadora de GCP antes de presupuestar; cambian
y dependen de la región y de los créditos de tu cuenta):

| Concepto | Supuesto | 2 semanas (336 h) | 5 semanas (840 h) |
|---|---|---|---|
| VM e2-small | ~0.0168 USD/h, encendida todo el tiempo | ~5.6 USD | ~14.1 USD |
| Disco pd-balanced 30 GB | ~0.10 USD/GB-mes | ~1.4 USD | ~3.5 USD |
| IP externa fija en uso | ~0.005 USD/h | ~1.7 USD | ~4.2 USD |
| Salida a internet | 15 equipos, 2 a 4 GB/semana en total, ~0.12 USD/GB | ~0.5 a 1 USD | ~1.2 a 2.4 USD |
| Instantáneas | ~0.05 USD/GB-mes, 7 días de retención | < 0.5 USD | < 1 USD |
| **Total** | | **~9 a 10 USD** | **~24 a 25 USD** |

Notas:

- E2 no tiene descuento por uso sostenido; un compromiso de uso no vale la pena para 5 semanas.
- Con `pd-standard` el disco baja a ~0.04 USD/GB-mes (~1.4 USD en 5 semanas) a cambio de menos IOPS; con 15 equipos
  leyendo MySQL, `pd-balanced` es más seguro.
- La salida a internet depende de cuántas veces relean los equipos todo el estado de MySQL; con extracción
  incremental es mínima.
