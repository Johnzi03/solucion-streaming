# CENTINELA — Plataforma Federal de Bloqueos Judiciales en Vivo

Solución integral desarrollada en **FastAPI**, **PostgreSQL** y **Python (fpdf2)** para la gestión en tiempo real de expedientes judiciales por retransmisión ilegal de eventos deportivos, firma criptográfica del magistrado de turno, despacho paralelo de órdenes a proveedores de Internet (ISPs), cadena de auditoría inmutable (hash-chain) y emisión de oficios judiciales en PDF.

---

## Estructura del Repositorio

- `datos/`: Catálogos base del dominio (jueces, proveedores ISP, partidos).
- `simulador/`: Simulador oficial del partido, detecciones y nodos ISP (puerto 8080).
- `plataforma/`: Backend FastAPI e interfaz web operativa (puerto 8001).
  - `app/main.py`: Endpoints API, login judicial, despacho a ISPs y descarga PDF.
  - `app/db.py`: Conexión a PostgreSQL y verificación de hash-chain.
  - `app/ingesta.py`: Sincronización en vivo con el simulador.
  - `app/judicial.py`: Control de turno y firma de expedientes.
  - `app/proveedores.py`: Envío paralelo y levantamiento de órdenes en los 4 ISPs.
  - `app/export.py`: Generador oficial de oficios judiciales en PDF (fpdf2).
  - `app/templates/index.html`: Panel de control web en tiempo real.
  - `requirements.txt`: Dependencias Python (FastAPI, Uvicorn, psycopg, httpx, fpdf2).
  - `schema.sql`: Esquema DDL de PostgreSQL (tablas, índices y restricciones).
- `evaluacion/`: Script de verificación automática y verdad práctica.
- `entrega/`: Evidencia exportada (expediente_ejemplo.json y registro_auditoria.json).
- `salida/`: Registro de eventos generados durante la simulación.

---

## Requisitos Previos

- **Python 3.11+**
- **PostgreSQL 14+** activo en local (`localhost:5432`)

---

## Guía de Instalación y Ejecución

### 1. Crear la base de datos y cargar el esquema en PostgreSQL
    psql -U postgres -c "CREATE DATABASE centinela;"
    psql -U postgres -d centinela -f plataforma/schema.sql

*(Si tu usuario, contraseña o puerto de PostgreSQL difieren de los valores por defecto, configuralos mediante las variables de entorno `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` y `PGPASSWORD`)*

### 2. Instalar dependencias de Python
    pip install -r plataforma/requirements.txt

### 3. Iniciar el simulador oficial (Terminal 1)
    python simulador/servidor.py --acelerar 15

### 4. Iniciar la plataforma CENTINELA (Terminal 2)
    cd plataforma
    uvicorn app.main:app --reload --port 8001

Abrí `http://127.0.0.1:8001/` en el navegador.

### 5. Ejecutar verificación de métricas (Opcional)
    python evaluacion/verificar.py

---

## Funcionalidades Implementadas

- **Ingesta y verificación en vivo:** Monitoreo continuo del minuto de partido, extracción de marca de agua, detección de IP compartida y cálculo de hash SHA-256 de la evidencia.
- **Control de turno y firma judicial:** Autenticación del magistrado de turno vigente según el minuto del encuentro para aprobar, rechazar o levantar bloqueos.
- **Despacho paralelo a ISPs:** Notificación simultánea a los 4 proveedores (andina, aurora, cumbre, valle) con firma HMAC y seguimiento de estado (RECIBIDA / APLICADA).
- **Auditoría inmutable (Hash-Chain):** Registro encadenado criptográficamente en PostgreSQL con verificación de integridad desde la interfaz.
- **Exportación de Oficios en PDF y JSON:** Descarga directa desde cada expediente del **Oficio Judicial en PDF** (`/api/expediente/{id}/pdf`) con evidencia, firma del juez, tabla de ISPs y estado de la cadena de auditoría, además de los entregables JSON en `entrega/`.
