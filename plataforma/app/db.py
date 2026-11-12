import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/qualydata")
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def get_conn() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def inicializar_db(limpiar_datos: bool = False) -> None:
    """Crea las tablas si no existen. Si limpiar_datos=True, reinicia para un nuevo partido."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn() as conn:
        conn.execute(sql)
        if limpiar_datos:
            conn.execute("TRUNCATE TABLE ordenes_proveedor, expedientes, reclamos, auditoria_hash_chain RESTART IDENTITY CASCADE;")
        conn.commit()


def sha256_canonico(data: Any) -> str:
    """Serializa cualquier estructura en JSON determinístico y devuelve su huella SHA-256."""
    serializado = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serializado.encode("utf-8")).hexdigest()


def registrar_auditoria(
    conn: psycopg.Connection,
    minuto: float,
    evento: str,
    entidad_id: str,
    actor: str,
    payload: Dict[str, Any],
) -> str:
    """Inserta un evento encadenado al hash anterior dentro de la misma transacción SQL."""
    minuto_norm = round(float(minuto), 3)
    with conn.cursor() as cur:
        # Bloqueamos la última fila para evitar bifurcaciones de hash si hay concurrencia
        cur.execute("SELECT hash_actual FROM auditoria_hash_chain ORDER BY seq DESC LIMIT 1 FOR UPDATE")
        row = cur.fetchone()
        hash_anterior = row["hash_actual"] if row else "0" * 64

        bloque = {
            "minuto_simulado": minuto_norm,
            "evento": evento,
            "entidad_id": entidad_id,
            "actor": actor,
            "payload": payload,
            "hash_anterior": hash_anterior,
        }
        hash_actual = sha256_canonico(bloque)

        cur.execute(
            """
            INSERT INTO auditoria_hash_chain
                (minuto_simulado, evento, entidad_id, actor, payload_json, hash_anterior, hash_actual)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (minuto_norm, evento, entidad_id, actor, json.dumps(payload, ensure_ascii=False), hash_anterior, hash_actual),
        )
    return hash_actual


def verificar_integridad_auditoria() -> Dict[str, Any]:
    """Recorre toda la tabla auditoria_hash_chain y valida que ningún registro haya sido alterado."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM auditoria_hash_chain ORDER BY seq ASC")
            filas: List[Dict[str, Any]] = cur.fetchall()

    hash_esperado_anterior = "0" * 64
    errores = []

    for fila in filas:
        if fila["hash_anterior"] != hash_esperado_anterior:
            errores.append(f"Seq {fila['seq']}: hash_anterior roto (esperado {hash_esperado_anterior}, hallado {fila['hash_anterior']})")

        bloque = {
            "minuto_simulado": round(float(fila["minuto_simulado"]), 3),
            "evento": fila["evento"],
            "entidad_id": fila["entidad_id"],
            "actor": fila["actor"],
            "payload": fila["payload_json"],
            "hash_anterior": fila["hash_anterior"],
        }
        recalculado = sha256_canonico(bloque)
        if recalculado != fila["hash_actual"]:
            errores.append(f"Seq {fila['seq']}: contenido alterado (hash_actual no coincide)")

        hash_esperado_anterior = fila["hash_actual"]

    return {
        "integro": len(errores) == 0,
        "total_registros": len(filas),
        "ultimo_hash": hash_esperado_anterior,
        "errores": errores,
    }


if __name__ == "__main__":
    # Prueba rápida del Módulo 1
    inicializar_db(limpiar_datos=True)
    with get_conn() as conn:
        h1 = registrar_auditoria(conn, 0.0, "SISTEMA_INICIADO", "SYS-1", "orquestador", {"version": "1.0"})
        h2 = registrar_auditoria(conn, 2.5, "DETECCION_RECIBIDA", "DET-1", "ingesta", {"ip": "203.0.113.10"})
        conn.commit()

    print("Hash 1:", h1)
    print("Hash 2:", h2)
    print("Verificación de integridad:", verificar_integridad_auditoria())