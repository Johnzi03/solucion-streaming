import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi.responses import HTMLResponse, JSONResponse, Response
from app.export import generar_pdf_expediente

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from urllib.parse import parse_qs
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
SESION_JUEZ_ACTUAL: Optional[str] = None


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
                if not gestor_isp.proveedores:
                    await gestor_isp.cargar_proveedores(client)

                nuevos_exp = await ingesta.consultar_nuevas_detecciones(client)

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


# --- ADAPTADOR DE COMPATIBILIDAD CON FRONTEND CENTINELA (index.html) ---

async def _leer_payload(request: Request) -> Dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    texto = raw.decode("utf-8", errors="ignore").strip()
    # 1. Intentar JSON
    if texto.startswith("{"):
        try:
            data = json.loads(texto)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    # 2. Intentar query/form urlencoded (ej: usuario=jueza-laura-ficticia&password=...)
    parsed = parse_qs(texto)
    if parsed:
        return {k: (v if len(v) > 1 else v[0]) for k, v in parsed.items()}
    return {}

def _map_estado_caso(estado: str) -> str:
    return {
        "PENDIENTE_FIRMA": "pendiente",
        "APROBADO": "aprobado",
        "RECHAZADO": "rechazado",
    }.get(estado, str(estado or "pendiente").lower())


def _map_estado_orden(estado: str) -> str:
    return {
        "APLICADA": "aplicada",
        "VENCIDA": "vencida",
        "LEVANTADA": "levantada",
        "RECIBIDA": "recibida",
        "FALLIDA": "fallida",
        "ERROR_ENVIO": "error_envio",
    }.get(estado, str(estado or "enviando").lower())


@app.get("/api/status")
async def api_status():
    raw = await api_dashboard()
    estado_sim = raw.get("estado_simulador") or {}
    partido_obj = raw.get("partido") or {}

    casos = []
    for e in raw.get("expedientes") or []:
        if e.get("estado") == "DESCARTADO":
            continue
        dns_info = e.get("dns_pasivo_json") or {}
        if isinstance(dns_info, str):
            try:
                dns_info = json.loads(dns_info)
            except Exception:
                dns_info = {}
        casos.append({
            "id": e.get("id"),
            "estado": _map_estado_caso(e.get("estado")),
            "min": round(float(e.get("minuto_deteccion") or 0), 1),
            "host": e.get("host_stream") or "",
            "ip": e.get("ip_servidor") or "",
            "asn": dns_info.get("asn") or "N/D",
            "compartida": bool(e.get("ip_compartida")),
            "sha256": e.get("hash_evidencia") or "",
            "juez_id": e.get("juez_id") or "",
        })

    ordenes = []
    for o in raw.get("ordenes") or []:
        ordenes.append({
            "orden_id": o.get("orden_id") or o.get("id"),
            "proveedor": o.get("proveedor_id") or o.get("proveedor"),
            "estado": _map_estado_orden(o.get("estado")),
            "objetivo": o.get("objetivo") or "",
            "intento": o.get("intento") or 1,
            "juez": o.get("juez_id") or "",
            "firmada_minuto": o.get("recibida_min"),
        })

    proveedores = []
    for p in gestor_isp.proveedores or []:
        proveedores.append({
            "id": p.get("id"),
            "nombre": p.get("nombre") or p.get("id"),
            "tamano": p.get("tamano") or "ISP",
            "capacidades": p.get("capacidades") or ["dominio"],
            "clientes": p.get("clientes") or 0,
        })

    reclamos = []
    for r in raw.get("reclamos") or []:
        if str(r.get("estado", "")).upper() != "RESUELTO":
            reclamos.append({
                "id": r.get("id"),
                "dominio": r.get("dominio") or "",
                "mensaje": r.get("motivo") or r.get("mensaje") or "Reclamo de bloqueo legítimo",
                "proveedor": r.get("proveedor_id") or "",
                "minuto": r.get("minuto_reclamo") or 0,
            })

    juzgados_map: Dict[str, Dict[str, Any]] = {}
    for j_id, j_info in JUECES_MAP.items():
        trib = j_info.get("juzgado_nombre") or "Juzgado Federal de Turno"
        juzgados_map.setdefault(trib, {"nombre": trib, "jueces": []})
        juzgados_map[trib]["jueces"].append({
            "id": j_id,
            "nombre": j_info.get("juez_nombre") or j_id,
        })

    return {
        "estado": estado_sim,
        "partido": partido_obj.get("nombre") or "Esperando datos del partido...",
        "casos": casos,
        "ordenes": ordenes,
        "proveedores": proveedores,
        "reclamos": reclamos,
        "auditoria_eventos": (raw.get("integridad") or {}).get("total_registros", 0),
        "juez_login": SESION_JUEZ_ACTUAL,
        "jueces": {"juzgados": list(juzgados_map.values())},
    }


@app.post("/api/login")
async def api_login(request: Request):
    global SESION_JUEZ_ACTUAL
    data = await _leer_payload(request)
    usuario = str(data.get("usuario") or "").strip()

    # Busca por juez_id exacto o por coincidencia con juez_nombre
    juez_encontrado = None
    if usuario in JUECES_MAP:
        juez_encontrado = usuario
    else:
        for j_id, info in JUECES_MAP.items():
            if usuario.lower() in (info.get("juez_nombre", "").lower(), j_id.lower()):
                juez_encontrado = j_id
                break

    if not juez_encontrado:
        return JSONResponse(
            status_code=400,
            content={"error": f"Juez no reconocido: '{usuario}'. Disponibles: {list(JUECES_MAP.keys())}"},
        )

    SESION_JUEZ_ACTUAL = juez_encontrado
    return {"ok": True, "juez": juez_encontrado}

@app.post("/api/logout")
async def api_logout():
    global SESION_JUEZ_ACTUAL
    SESION_JUEZ_ACTUAL = None
    return {"ok": True}


@app.post("/api/aprobar")
async def api_aprobar_form(request: Request):
    if not SESION_JUEZ_ACTUAL:
        return JSONResponse(status_code=401, content={"error": "Iniciá sesión como juez de turno"})
    data = await _leer_payload(request)
    raw_ids = data.get("expediente") or []
    ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]
    aprobados = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        for exp_id in ids:
            try:
                await aprobar_y_despachar(client, str(exp_id), SESION_JUEZ_ACTUAL)
                aprobados.append(str(exp_id))
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    return {"aprobados": aprobados}


@app.post("/api/rechazar")
async def api_rechazar_form(request: Request):
    if not SESION_JUEZ_ACTUAL:
        return JSONResponse(status_code=401, content={"error": "Iniciá sesión como juez de turno"})
    data = await _leer_payload(request)
    exp_id = str(data.get("expediente") or "")
    try:
        await api_rechazar(exp_id, FirmaRequest(juez_id=SESION_JUEZ_ACTUAL))
        return {"ok": True}
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.post("/api/levantar")
async def api_levantar_form(request: Request):
    if not SESION_JUEZ_ACTUAL:
        return JSONResponse(status_code=401, content={"error": "Iniciá sesión como juez de turno"})
    data = await _leer_payload(request)
    reclamo_id = str(data.get("reclamo") or "")
    try:
        res = await api_resolver_reclamo(reclamo_id, FirmaRequest(juez_id=SESION_JUEZ_ACTUAL))
        return {"levantadas": res.get("ordenes_levantadas", [])}
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.get("/api/expediente/{exp_id}")
async def api_expediente_detalle(exp_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM expedientes WHERE id = %s", (exp_id,))
            exp = cur.fetchone()
    if not exp:
        return JSONResponse(status_code=404, content={"error": "Expediente no encontrado"})
    return {
        "id": exp.get("id"),
        "partido": exp.get("partido_id"),
        "clasificacion": "pirata" if exp.get("estado") != "DESCARTADO" else "legitimo",
        "sha256_captura": exp.get("hash_evidencia"),
        "bytes_captura": 0,
        "captura_archivo": f"{exp.get('deteccion_id')}.ts",
        "recibido_utc": str(exp.get("creado_en") or ""),
        "minuto_recibido": exp.get("minuto_deteccion"),
        "deteccion": {
            "host_stream": exp.get("host_stream"),
            "marca_de_agua_extraida": exp.get("marca_agua_extraida"),
            "stream_url": f"https://{exp.get('host_stream')}/live.m3u8",
            "ip_servidor": exp.get("ip_servidor"),
            "promocion": exp.get("tipo_bloqueo_recomendado"),
        },
        "actuaciones_judiciales": {
            "estado": _map_estado_caso(exp.get("estado")),
            "juez_id": exp.get("juez_id"),
            "minuto_firma": exp.get("minuto_firma"),
        },
    }


@app.get("/api/verificar")
async def api_verificar_alias():
    res = verificar_integridad_auditoria()
    return {"integridad": res.get("integro", False), "eventos": res.get("total_registros", 0)}


@app.post("/api/exportar")
async def api_exportar_alias():
    await api_exportar_entrega()
    return {"carpeta": "entrega/"}


# --- ENDPOINTS REST ORIGINALES ---

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

@app.get("/api/expediente/{exp_id}/pdf")
async def api_descargar_pdf_expediente(exp_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM expedientes WHERE id = %s", (exp_id,))
            exp = cur.fetchone()
            if not exp:
                raise HTTPException(status_code=404, detail="Expediente no encontrado")
            cur.execute("SELECT * FROM ordenes_proveedor WHERE expediente_id = %s ORDER BY proveedor_id ASC", (exp_id,))
            ords = cur.fetchall()
            cur.execute("SELECT * FROM auditoria_hash_chain ORDER BY seq ASC")
            logs = cur.fetchall()

    integridad = verificar_integridad_auditoria()
    juez_info = JUECES_MAP.get(exp.get("juez_id") or "", {})
    pdf_bytes = generar_pdf_expediente(exp, ords, logs, integridad, juez_info)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="oficio_{exp_id}.pdf"'},
    )

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