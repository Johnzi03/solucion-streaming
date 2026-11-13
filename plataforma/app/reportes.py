"""Generación y persistencia de actas diarias de CENTINELA.

Las firmas que registra este módulo son intervenciones DEMO auditadas. No son
firmas digitales PDF, no contienen claves privadas y no pretenden validez legal.
"""
from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak

from app.db import get_conn, registrar_auditoria, sha256_canonico

TZ = ZoneInfo("America/Argentina/Jujuy")
REPO_DIR = Path(__file__).resolve().parents[2]
ACTAS_DIR = REPO_DIR / "entrega" / "actas"


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def consolidar_jornada(conn, jornada_id: str, minuto_simulado: Optional[float] = None) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM expedientes ORDER BY minuto_deteccion, id")
        expedientes = [_jsonable(dict(x)) for x in cur.fetchall()]
        cur.execute("SELECT * FROM ordenes_proveedor ORDER BY recibida_min, orden_id")
        ordenes = [_jsonable(dict(x)) for x in cur.fetchall()]
        cur.execute("SELECT * FROM reclamos ORDER BY minuto_reclamo, id")
        reclamos = [_jsonable(dict(x)) for x in cur.fetchall()]
        cur.execute("SELECT * FROM auditoria_hash_chain ORDER BY seq")
        auditoria = [_jsonable(dict(x)) for x in cur.fetchall()]
    return {
        "jornada_id": jornada_id,
        "zona_horaria": str(TZ),
        "generado_en": datetime.now(TZ).isoformat(),
        "minuto_simulado": minuto_simulado,
        "expedientes": expedientes,
        "ordenes": ordenes,
        "reclamos": reclamos,
        "auditoria": auditoria,
        "estadisticas": {
            "expedientes": len(expedientes),
            "aprobados": sum(x.get("estado") == "APROBADO" for x in expedientes),
            "ordenes": len(ordenes),
            "ordenes_aplicadas": sum(x.get("estado") in ("aplicada", "vencida") for x in ordenes),
            "reclamos": len(reclamos),
        },
    }


def _tabla(rows, widths=None):
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#263b57")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#b7c4d4")),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def render_pdf(snapshot: Dict[str, Any], acta_id: str, hash_datos: str, pdf_path: Path) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    s = []
    s += [Paragraph("CENTINELA", styles["Title"]), Paragraph("ACTA DIGITAL DIARIA · DEMO", styles["Heading2"]), Spacer(1, 10)]
    s += [Paragraph(f"Acta: {acta_id}<br/>Jornada: {snapshot['jornada_id']}<br/>Generación real: {snapshot['generado_en']}<br/>Zona: {snapshot['zona_horaria']}<br/>Minuto simulado: {snapshot.get('minuto_simulado')}<br/>SHA-256 de datos consolidados: {hash_datos}", styles["BodyText"]), PageBreak()]
    st = snapshot["estadisticas"]
    s += [Paragraph("Resumen de estadísticas", styles["Heading1"]), _tabla([["Indicador", "Valor"], *[[k, str(v)] for k, v in st.items()]], [90 * mm, 50 * mm]), Spacer(1, 10)]
    s += [Paragraph("Detalle de tickets / expedientes", styles["Heading1"])]
    ex = [["ID", "Min", "Host", "Estado"]] + [[x["id"], str(x["minuto_deteccion"]), x["host_stream"], x["estado"]] for x in snapshot["expedientes"]]
    s += [_tabla(ex or [["ID", "Min", "Host", "Estado"]], [35 * mm, 20 * mm, 75 * mm, 30 * mm]), PageBreak()]
    s += [Paragraph("Resultado de órdenes", styles["Heading1"])]
    oo = [["Orden", "Proveedor", "Objetivo", "Estado"]] + [[x["orden_id"], x["proveedor_id"], x["objetivo"], x["estado"]] for x in snapshot["ordenes"]]
    s += [_tabla(oo or [["Orden", "Proveedor", "Objetivo", "Estado"]], [38 * mm, 30 * mm, 70 * mm, 25 * mm]), Spacer(1, 10)]
    s += [Paragraph("Anexo de auditoría", styles["Heading1"]), Paragraph(f"Eventos incluidos: {len(snapshot['auditoria'])}. Último evento: {snapshot['auditoria'][-1]['seq'] if snapshot['auditoria'] else 'ninguno'}.", styles["BodyText"])]
    audit = [["Seq", "Evento", "Entidad", "Hash"]] + [[str(x["seq"]), x["evento"], x["entidad_id"], x["hash_actual"][:16] + "…"] for x in snapshot["auditoria"]]
    s += [_tabla(audit or [["Seq", "Evento", "Entidad", "Hash"]], [15 * mm, 48 * mm, 50 * mm, 48 * mm])]
    doc.build(s)


def crear_acta(jornada_id: str, minuto_simulado: Optional[float] = None) -> Dict[str, Any]:
    ACTAS_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM actas_diarias WHERE jornada_id=%s", (jornada_id,))
            existing = cur.fetchone()
            if existing:
                return _jsonable(dict(existing))
        snapshot = consolidar_jornada(conn, jornada_id, minuto_simulado)
        digest = sha256_canonico(snapshot)
        acta_id = str(uuid.uuid4())
        nombre = f"acta-{jornada_id}-{acta_id}.pdf".replace("/", "_")
        path = ACTAS_DIR / nombre
        render_pdf(snapshot, acta_id, digest, path)
        pdf_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ultimo = snapshot["auditoria"][-1] if snapshot["auditoria"] else None
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO actas_diarias
                (acta_id,jornada_id,datos_hash_sha256,ultimo_evento_auditoria_seq,ultimo_evento_auditoria_hash,snapshot_json,pdf_path,minuto_simulado,pdf_sha256)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
                (acta_id, jornada_id, digest, ultimo.get("seq") if ultimo else None,
                 ultimo.get("hash_actual") if ultimo else None, json.dumps(snapshot, ensure_ascii=False), str(path), minuto_simulado, pdf_digest))
            registrar_auditoria(conn, float(minuto_simulado or 0), "ACTA_GENERADA", acta_id, "sistema", {"jornada_id": jornada_id, "datos_hash_sha256": digest})
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM actas_diarias WHERE acta_id=%s", (acta_id,))
            return _jsonable(dict(cur.fetchone()))
