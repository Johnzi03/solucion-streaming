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

-- 5. Actas diarias: el snapshot es la fuente inmutable del PDF generado.
CREATE TABLE IF NOT EXISTS actas_diarias (
    acta_id UUID PRIMARY KEY,
    jornada_id VARCHAR(128) NOT NULL UNIQUE,
    estado VARCHAR(24) NOT NULL DEFAULT 'BORRADOR', -- BORRADOR | CERRADA
    creada_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cerrada_en TIMESTAMPTZ,
    zona_horaria VARCHAR(64) NOT NULL DEFAULT 'America/Argentina/Jujuy',
    minuto_simulado NUMERIC(8, 3),
    datos_hash_sha256 VARCHAR(64) NOT NULL,
    ultimo_evento_auditoria_seq BIGINT,
    ultimo_evento_auditoria_hash VARCHAR(64),
    snapshot_json JSONB NOT NULL,
    pdf_path TEXT NOT NULL,
    pdf_sha256 VARCHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS acta_firmas (
    id BIGSERIAL PRIMARY KEY,
    acta_id UUID NOT NULL REFERENCES actas_diarias(acta_id) ON DELETE RESTRICT,
    rol VARCHAR(16) NOT NULL, -- JUEZ | ESCRIBANO
    identidad VARCHAR(128) NOT NULL,
    resultado VARCHAR(32) NOT NULL, -- DEMO_VERIFICADA | RECHAZADA
    modo VARCHAR(16) NOT NULL DEFAULT 'DEMO',
    certificado_valido BOOLEAN NOT NULL DEFAULT FALSE,
    cadena_validada BOOLEAN NOT NULL DEFAULT FALSE,
    revocacion_comprobada BOOLEAN NOT NULL DEFAULT FALSE,
    verificado_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detalle_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (acta_id, rol)
);

ALTER TABLE actas_diarias ADD COLUMN IF NOT EXISTS pdf_sha256 VARCHAR(64);

CREATE OR REPLACE FUNCTION impedir_mutacion_acta_cerrada() RETURNS trigger AS $$
BEGIN
    IF OLD.estado = 'CERRADA' THEN
        RAISE EXCEPTION 'El acta % está cerrada y es inmutable', OLD.acta_id;
    END IF;
    IF NEW.snapshot_json IS DISTINCT FROM OLD.snapshot_json
       OR NEW.datos_hash_sha256 IS DISTINCT FROM OLD.datos_hash_sha256
       OR NEW.pdf_path IS DISTINCT FROM OLD.pdf_path
       OR NEW.jornada_id IS DISTINCT FROM OLD.jornada_id THEN
        RAISE EXCEPTION 'El snapshot del acta % es inmutable', OLD.acta_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_acta_inmutable ON actas_diarias;
CREATE TRIGGER trg_acta_inmutable BEFORE UPDATE ON actas_diarias
FOR EACH ROW EXECUTE FUNCTION impedir_mutacion_acta_cerrada();

CREATE INDEX IF NOT EXISTS idx_actas_jornada ON actas_diarias(jornada_id);
CREATE INDEX IF NOT EXISTS idx_acta_firmas_acta ON acta_firmas(acta_id);
