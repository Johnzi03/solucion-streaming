-- 1. Expedientes judiciales generados por cada detección
CREATE TABLE IF NOT EXISTS expedientes (
    id VARCHAR(64) PRIMARY KEY,
    deteccion_id VARCHAR(64) UNIQUE NOT NULL,
    minuto_deteccion NUMERIC(8, 3) NOT NULL,
    host_stream VARCHAR(255) NOT NULL,
    ip_servidor VARCHAR(64) NOT NULL,
    marca_agua_extraida VARCHAR(128) NOT NULL,
    es_senial_oficial BOOLEAN NOT NULL,
    es_licenciatario BOOLEAN NOT NULL,
    dns_pasivo_json JSONB NOT NULL,
    ip_compartida BOOLEAN NOT NULL,
    tipo_bloqueo_recomendado VARCHAR(16) NOT NULL, -- 'ip' | 'dominio' | 'ninguno'
    objetivo_recomendado VARCHAR(255),
    hash_evidencia VARCHAR(64) NOT NULL,
    estado VARCHAR(32) NOT NULL DEFAULT 'PENDIENTE_FIRMA', -- PENDIENTE_FIRMA | APROBADO | RECHAZADO | DESCARTADO
    juzgado_id VARCHAR(64),
    juez_id VARCHAR(64),
    minuto_firma NUMERIC(8, 3),
    firma_judicial VARCHAR(128),
    creado_en TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Órdenes individuales enviadas a cada ISP (soporta múltiples intentos ante fallas)
CREATE TABLE IF NOT EXISTS ordenes_proveedor (
    orden_id VARCHAR(80) PRIMARY KEY,
    expediente_id VARCHAR(64) NOT NULL REFERENCES expedientes(id) ON DELETE CASCADE,
    proveedor_id VARCHAR(64) NOT NULL,
    intento INTEGER NOT NULL DEFAULT 1,
    tipo VARCHAR(16) NOT NULL, -- 'ip' | 'dominio'
    objetivo VARCHAR(255) NOT NULL,
    vence_min INTEGER NOT NULL,
    juzgado_id VARCHAR(64) NOT NULL,
    juez_id VARCHAR(64) NOT NULL,
    firma VARCHAR(128) NOT NULL,
    estado VARCHAR(32) NOT NULL DEFAULT 'recibida', -- recibida | aplicada | fallida | vencida | levantada
    recibida_min NUMERIC(8, 3),
    aplicada_min NUMERIC(8, 3),
    finalizada_min NUMERIC(8, 3),
    motivo_falla TEXT,
    UNIQUE (expediente_id, proveedor_id, intento)
);

-- 3. Reclamos de sitios legítimos afectados
CREATE TABLE IF NOT EXISTS reclamos (
    id VARCHAR(64) PRIMARY KEY,
    minuto_reclamo NUMERIC(8, 3) NOT NULL,
    dominio VARCHAR(255) NOT NULL,
    proveedor_id VARCHAR(64) NOT NULL,
    mensaje TEXT,
    orden_afectada_id VARCHAR(80),
    estado VARCHAR(32) NOT NULL DEFAULT 'PENDIENTE', -- PENDIENTE | RESUELTO
    juez_resolucion_id VARCHAR(64),
    minuto_resolucion NUMERIC(8, 3)
);

-- 4. Registro de auditoría inalterable encadenado por SHA-256 (Hash Chain)
CREATE TABLE IF NOT EXISTS auditoria_hash_chain (
    seq BIGSERIAL PRIMARY KEY,
    minuto_simulado NUMERIC(8, 3) NOT NULL,
    timestamp_real TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    evento VARCHAR(64) NOT NULL,
    entidad_id VARCHAR(80) NOT NULL,
    actor VARCHAR(64) NOT NULL,
    payload_json JSONB NOT NULL,
    hash_anterior VARCHAR(64) NOT NULL,
    hash_actual VARCHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_expedientes_estado ON expedientes(estado);
CREATE INDEX IF NOT EXISTS idx_ordenes_estado ON ordenes_proveedor(estado);
CREATE INDEX IF NOT EXISTS idx_reclamos_estado ON reclamos(estado);