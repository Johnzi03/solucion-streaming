# CENTINELA — Plataforma Federal de Bloqueos Judiciales en Vivo

Solución integral desarrollada en **FastAPI**, **PostgreSQL** y **Python (`fpdf2`)** para la gestión en tiempo real de expedientes judiciales por retransmisión ilegal de eventos deportivos, firma criptográfica del magistrado de turno, despacho paralelo de órdenes a proveedores de Internet (ISPs), cadena de auditoría inmutable (`hash-chain`) y emisión de oficios judiciales en PDF.

---

## Estructura del Repositorio

```text
solucion-streaming/
├── datos/                  # Catálogos base del dominio (jueces, proveedores ISP, partidos)
├── simulador/              # Simulador oficial del partido, detecciones y nodos ISP (puerto 8080)
├── plataforma/             # Backend FastAPI + Interfaz Web operativa (puerto 8001)[cite: 15]
│   ├── app/
│   │   ├── main.py         # Endpoints API, login judicial, despacho a ISPs y descarga PDF[cite: 15]
│   │   ├── db.py           # Conexión a PostgreSQL y verificación de hash-chain[cite: 15]
│   │   ├── ingesta.py      # Sincronización en vivo con el simulador[cite: 15]
│   │   ├── judicial.py     # Control de turno y firma de expedientes[cite: 15]
│   │   ├── proveedores.py  # Envío paralelo y levantamiento de órdenes en los 4 ISPs[cite: 15]
│   │   ├── export.py       # Generador oficial de oficios judiciales en PDF (fpdf2)[cite: 15]
│   │   └── templates/
│   │       └── index.html  # Panel de control web en tiempo real[cite: 15]
│   ├── requirements.txt    # Dependencias Python (FastAPI, Uvicorn, psycopg, httpx, fpdf2)[cite: 15]
│   └── schema.sql          # Esquema DDL de PostgreSQL (tablas, índices y restricciones)[cite: 15]
├── evaluacion/             # Script de verificación automática y verdad práctica[cite: 15]
├── entrega/                # Evidencia exportada (expediente_ejemplo.json y registro_auditoria.json)[cite: 15]
└── salida/                 # Registro de eventos generados durante la simulación[cite: 15]
