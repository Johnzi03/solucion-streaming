import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

import httpx
from fastapi import HTTPException

from app.db import get_conn, registrar_auditoria

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SIMULADOR_URL = os.getenv("SIMULADOR_URL", "http://localhost:8080").rstrip("/")
JUZGADOS_PATH = REPO_DIR / "datos" / "juzgados.json"


def cargar_jueces() -> Dict[str, Dict[str, str]]:
    data = json.loads(JUZGADOS_PATH.read_text(encoding="utf-8"))
    mapa: Dict[str, Dict[str, str]] = {}
    for jz in data["juzgados"]:
        for j in jz["jueces"]:
            mapa[j["id"]] = {
                "juez_id": j["id"],
                "juez_nombre": j["nombre"],
                "clave_firma": j["clave_firma_demo"],
                "juzgado_id": jz["id"],
                "juzgado_nombre": jz["nombre"],
            }
    return mapa


JUECES_MAP = cargar_jueces()


def firmar_hmac(clave: str, mensaje: str) -> str:
    return hmac.new(clave.encode("utf-8"), mensaje.encode("utf-8"), hashlib.sha256).hexdigest()


def firmar_orden_isp(juez_id: str, orden_id: str, tipo: str, objetivo: str, vence_min: int) -> str:
    juez = JUECES_MAP[juez_id]
    mensaje = f"{orden_id}|{tipo}|{objetivo}|{vence_min}|{juez_id}"
    return firmar_hmac(juez["clave_firma"], mensaje)


def firmar_levantamiento(juez_id: str, orden_id: str) -> str:
    juez = JUECES_MAP[juez_id]
    mensaje = f"LEVANTAR|{orden_id}|{juez_id}"
    return firmar_hmac(juez["clave_firma"], mensaje)


async def validar_juez_de_turno(client: httpx.AsyncClient, juez_id: str, entidad_id: str) -> Tuple[Dict[str, str], float]:
    """Verifica que el juez exista y esté de turno en el minuto simulado actual."""
    if juez_id not in JUECES_MAP:
        raise HTTPException(status_code=403, detail="Juez no registrado en el padrón judicial")

    resp = await client.get(f"{SIMULADOR_URL}/estado")
    resp.raise_for_status()
    estado = resp.json()
    minuto_actual = float(estado["minuto_actual"])
    de_turno = estado.get("jueces_de_turno", [])

    if juez_id not in de_turno:
        with get_conn() as conn:
            registrar_auditoria(
                conn,
                minuto_actual,
                "INTENTO_FIRMA_FUERA_DE_TURNO",
                entidad_id,
                juez_id,
                {"jueces_habilitados": de_turno},
            )
            conn.commit()
        raise HTTPException(
            status_code=403,
            detail=f"Firma denegada: {juez_id} está fuera de turno en el minuto {minuto_actual}. En turno: {de_turno}",
        )

    return JUECES_MAP[juez_id], minuto_actual


async def aprobar_expediente_judicial(client: httpx.AsyncClient, exp_id: str, juez_id: str) -> Dict[str, Any]:
    """Valida turno, sella la aprobación del expediente en PostgreSQL y devuelve sus datos."""
    juez_info, minuto_actual = await validar_juez_de_turno(client, juez_id, exp_id)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM expedientes WHERE id = %s FOR UPDATE", (exp_id,))
            exp = cur.fetchone()
            if not exp:
                raise HTTPException(status_code=404, detail="Expediente inexistente")
            if exp["estado"] != "PENDIENTE_FIRMA":
                raise HTTPException(status_code=409, detail=f"El expediente ya fue resuelto ({exp['estado']})")

            firma_exp = firmar_hmac(
                juez_info["clave_firma"],
                f"APROBAR|{exp_id}|{exp['hash_evidencia']}|{juez_id}",
            )
            cur.execute(
                """
                UPDATE expedientes
                SET estado = 'APROBADO', juzgado_id = %s, juez_id = %s, minuto_firma = %s, firma_judicial = %s
                WHERE id = %s
                """,
                (juez_info["juzgado_id"], juez_id, minuto_actual, firma_exp, exp_id),
            )
            registrar_auditoria(
                conn,
                minuto_actual,
                "EXPEDIENTE_APROBADO_FIRMADO",
                exp_id,
                juez_id,
                {"juzgado_id": juez_info["juzgado_id"], "firma_judicial": firma_exp},
            )
        conn.commit()

    return {"expediente": dict(exp), "juez_info": juez_info, "minuto_firma": minuto_actual}
