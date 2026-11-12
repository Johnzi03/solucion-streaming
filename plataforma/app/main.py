import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.db import get_conn, inicializar_db, registrar_auditoria, verificar_integridad_auditoria
from app.ingesta import MotorIngesta
from app.judicial import JUECES_MAP, aprobar_expediente_judicial, validar_juez_de_turno
from app.proveedores import GestorProveedores

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SIMULADOR_URL = os.getenv("SIMULADOR_URL", "http://localhost:8080").rstrip("/")
AUTO_APROBAR_DEMO = os.getenv("AUTO_APROBAR_DEMO", "false").lower() == "true"

ingesta = MotorIngesta(SIMULADOR_URL)
gestor_isp = GestorProveedores(SIMULADOR_URL)
ejecutando_loop = True


async def aprobar_y_despachar(client: httpx.AsyncClient, exp_id: str, juez_id: str) -> Dict[str, Any]:
    """Ejecuta el paso judicial (firma) y dispara en paralelo las órdenes a todos los ISPs."""
    res_judicial = await aprobar_expediente_judicial(client, exp_id, juez_id)
    fin_partido = int(ingesta.partido.get("fin_min", 105))
    ordenes = await gestor_isp.despachar_expediente(
        client=client,
        exp=res_judicial["expediente"],
        juez_info=res_judicial["juez_info"],
        minuto_actual=res_judicial["minuto_firma"],
        fin_partido=fin_partido,
    )
    return {"expediente_id": exp_id, "estado": "APROBADO", "ordenes_emitidas": ordenes}


async def ciclo_orquestador() -> None:
    """Loop continuo: ingiere detecciones, vigila fallas en ISPs para reintentar y captura reclamos."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        while ejecutando_loop:
            try:
                if not ingesta.partido:
                    await ingesta.cargar_metadatos_partido(client)
                    await gestor_isp.cargar_proveedores(client)

                nuevos_exp = await ingesta.consultar_nuevas_detecciones(client)

                # Si está activo el modo auto-aprobación (útil para testear simulador acelerado), firma con el juez de turno
                if AUTO_APROBAR_DEMO and nuevos_exp:
                    est = (await client.get(f"{SIMULADOR_URL}/estado")).json()
                    de_turno = est.get("jueces_de_turno", [])
                    if de_turno:
                        with get_conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute("SELECT id FROM expedientes WHERE estado = 'PENDIENTE_FIRMA'")
                                pendientes = [r["id"] for r in cur.fetchall()]
                        for exp_id in pendientes:
                            try:
                                await aprobar_y_despachar(client, exp_id, de_turno[0])
                            except HTTPException:
                                pass

                fin_partido = int(ingesta.partido.get("fin_min", 105))
                await gestor_isp.sincronizar_y_reintentar(client, fin_partido)
                await ingesta.consultar_nuevos_reclamos(client)
            except Exception as exc:
                print(f"[Orquestador] Esperando simulador o reintentando ciclo: {exc}")

            await asyncio.sleep(0.3)


@asynccontextmanager
async def lifespan(app: FastAPI):
    inicializar_db(limpiar_datos=True)
    tarea = asyncio.create_task(ciclo_orquestador())
    yield
    global ejecutando_loop
    ejecutando_loop = False
    tarea.cancel()


app = FastAPI(title="Plataforma Federal de Bloqueos Judiciales en Vivo", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


class FirmaRequest(BaseModel):
    juez_id: str


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"jueces": list(JUECES_MAP.values())},
    )

@app.get("/api/dashboard")
async def api_dashboard():
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            estado_sim = (await client.get(f"{SIMULADOR_URL}/estado")).json()
        except Exception:
            estado_sim = {"minuto_actual": 0, "jueces_de_turno": [], "finalizado": False}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM expedientes ORDER BY minuto_deteccion DESC")
            expedientes = cur.fetchall()
            cur.execute("SELECT * FROM ordenes_proveedor ORDER BY recibida_min DESC")
            ordenes = cur.fetchall()
            cur.execute("SELECT * FROM reclamos ORDER BY minuto_reclamo DESC")
            reclamos = cur.fetchall()
            cur.execute("SELECT * FROM auditoria_hash_chain ORDER BY seq DESC LIMIT 30")
            auditoria = cur.fetchall()

    return {
        "estado_simulador": estado_sim,
        "partido": ingesta.partido,
        "expedientes": expedientes,
        "ordenes": ordenes,
        "reclamos": reclamos,
        "auditoria": auditoria,
        "integridad": verificar_integridad_auditoria(),
    }


@app.post("/api/expedientes/{exp_id}/aprobar")
async def api_aprobar(exp_id: str, body: FirmaRequest):
    async with httpx.AsyncClient(timeout=5.0) as client:
        return await aprobar_y_despachar(client, exp_id, body.juez_id)


@app.post("/api/expedientes/{exp_id}/aprobar-todos")
async def api_aprobar_todos(body: FirmaRequest):
    """Permite al juez de turno firmar en lote todos los expedientes pendientes visibles."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM expedientes WHERE estado = 'PENDIENTE_FIRMA' ORDER BY minuto_deteccion ASC")
            pendientes = [r["id"] for r in cur.fetchall()]

    aprobados = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        for exp_id in pendientes:
            res = await aprobar_y_despachar(client, exp_id, body.juez_id)
            aprobados.append(res["expediente_id"])
    return {"aprobados": aprobados, "total": len(aprobados)}


@app.post("/api/expedientes/{exp_id}/rechazar")
async def api_rechazar(exp_id: str, body: FirmaRequest):
    async with httpx.AsyncClient(timeout=3.0) as client:
        _, minuto_actual = await validar_juez_de_turno(client, body.juez_id, exp_id)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE expedientes SET estado = 'RECHAZADO', juez_id = %s, minuto_firma = %s WHERE id = %s",
                (body.juez_id, minuto_actual, exp_id),
            )
            registrar_auditoria(conn, minuto_actual, "EXPEDIENTE_RECHAZADO", exp_id, body.juez_id, {})
        conn.commit()
    return {"expediente_id": exp_id, "estado": "RECHAZADO"}


@app.post("/api/reclamos/{reclamo_id}/resolver")
async def api_resolver_reclamo(reclamo_id: str, body: FirmaRequest):
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            return await gestor_isp.levantar_orden_por_reclamo(client, reclamo_id, body.juez_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/exportar-entrega")
async def api_exportar_entrega():
    """Exporta expediente y registro de auditoría verificable a la carpeta entrega/."""
    entrega_dir = REPO_DIR / "entrega"
    entrega_dir.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM expedientes WHERE estado = 'APROBADO' ORDER BY minuto_deteccion ASC LIMIT 1")
            exp = cur.fetchone()
            cur.execute("SELECT * FROM ordenes_proveedor WHERE expediente_id = %s", (exp["id"],) if exp else ("",))
            ords = cur.fetchall()
            cur.execute("SELECT * FROM auditoria_hash_chain ORDER BY seq ASC")
            logs = cur.fetchall()

    integridad = verificar_integridad_auditoria()
    (entrega_dir / "expediente_ejemplo.json").write_text(
        json.dumps({"expediente": exp, "ordenes_emitidas": ords}, default=str, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (entrega_dir / "registro_auditoria.json").write_text(
        json.dumps({"integridad_verificada": integridad, "registros": logs}, default=str, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "archivos": ["entrega/expediente_ejemplo.json", "entrega/registro_auditoria.json"],
        "integridad": integridad,
    }