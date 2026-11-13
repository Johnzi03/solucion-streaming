from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
from fpdf import FPDF


def _limpiar_texto(texto: Any) -> str:
    if texto is None:
        return "N/D"
    return str(texto).encode("latin-1", "replace").decode("latin-1")


class OficioJudicialPDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(30, 41, 59)
        self.cell(0, 6, "CENTINELA - PLATAFORMA FEDERAL DE BLOQUEOS JUDICIALES EN VIVO", new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_font("Helvetica", "", 8)
        self.set_text_color(100, 116, 139)
        self.cell(0, 5, "Jurisdiccion Federal Ficticia | Reporte Oficial de Actuacion y Cadena de Custodia", new_x="LMARGIN", new_y="NEXT", align="C")
        self.ln(3)
        self.set_draw_color(203, 213, 225)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(100, 116, 139)
        tz_ar = timezone(timedelta(hours=-3))
        ahora = datetime.now(tz_ar).strftime("%d/%m/%Y %H:%M:%S (UTC-3)")
        self.cell(0, 10, f"Documento generado automaticamente el {ahora} | Pagina {self.page_no()}", align="C")


def generar_pdf_expediente(
    expediente: Dict[str, Any],
    ordenes: List[Dict[str, Any]],
    auditoria: List[Dict[str, Any]],
    integridad: Dict[str, Any],
    juez_info: Dict[str, str],
) -> bytes:
    pdf = OficioJudicialPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 8, _limpiar_texto(f"OFICIO JUDICIAL DE BLOQUEO - EXPEDIENTE {expediente.get('id')}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_fill_color(241, 245, 249)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, " 1. EVIDENCIA TECNICA Y DETECCION EN VIVO", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    filas_exp = [
        ("ID Expediente / Deteccion:", f"{expediente.get('id')} ({expediente.get('deteccion_id')})"),
        ("Partido / Evento:", _limpiar_texto(expediente.get("partido_id") or expediente.get("partido") or "Partido Oficial - Jurisdiccion Federal")),
        ("Minuto de Deteccion:", f"Minuto {expediente.get('minuto_deteccion')}'"),
        ("Host / Dominio Infractor:", _limpiar_texto(expediente.get("host_stream"))),
        ("Direccion IP Servidor:", _limpiar_texto(expediente.get("ip_servidor"))),
        ("IP Compartida:", "Si (Requiere cautela)" if expediente.get("ip_compartida") else "No (Aparentemente exclusiva)"),
        ("Marca de Agua Extraida:", _limpiar_texto(expediente.get("marca_agua_extraida"))),
        ("Bloqueo Recomendado:", f"{expediente.get('tipo_bloqueo_recomendado')} -> {expediente.get('objetivo_recomendado')}"),
        ("SHA-256 Evidencia:", _limpiar_texto(expediente.get("hash_evidencia"))),
    ]

    for clave, valor in filas_exp:
        pdf.set_x(10)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(52, 6, clave)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 6, _limpiar_texto(valor), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(3)

    pdf.set_x(10)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, " 2. RESOLUCION Y FIRMA DE AUTORIDAD JUDICIAL", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    juzgado_txt = juez_info.get("juzgado_nombre") or expediente.get("juzgado_id") or "Juzgado Federal de Turno"
    juez_txt = juez_info.get("juez_nombre") or expediente.get("juez_id") or "Sin firmar"

    filas_jud = [
        ("Estado Procesal:", _limpiar_texto(expediente.get("estado"))),
        ("Juzgado Interviniente:", _limpiar_texto(juzgado_txt)),
        ("Magistrado Firmante:", _limpiar_texto(juez_txt)),
        ("Minuto de Firma:", f"Minuto {expediente.get('minuto_firma')}'" if expediente.get("minuto_firma") is not None else "Pendiente"),
    ]

    for clave, valor in filas_jud:
        pdf.set_x(10)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(52, 6, clave)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 6, _limpiar_texto(valor), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(3)

    pdf.set_x(10)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, f" 3. ORDENES DE BLOQUEO DESPACHADAS A ISPs ({len(ordenes)} registros)", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    if not ordenes:
        pdf.set_x(10)
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 6, "No se registran ordenes despachadas para este expediente.", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_x(10)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(35, 6, "Proveedor ISP", border=1)
        pdf.cell(42, 6, "Orden ID", border=1)
        pdf.cell(48, 6, "Objetivo", border=1)
        pdf.cell(25, 6, "Estado", border=1)
        pdf.cell(40, 6, "Firma HMAC (parcial)", border=1, new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "", 8)
        for o in ordenes:
            pdf.set_x(10)
            firma_corta = str(o.get("firma") or "")[:18] + "..."
            pdf.cell(35, 6, _limpiar_texto(o.get("proveedor_id")), border=1)
            pdf.cell(42, 6, _limpiar_texto(o.get("orden_id")), border=1)
            pdf.cell(48, 6, _limpiar_texto(o.get("objetivo")), border=1)
            pdf.cell(25, 6, _limpiar_texto(str(o.get("estado")).upper()), border=1)
            pdf.cell(40, 6, _limpiar_texto(firma_corta), border=1, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)

    pdf.set_x(10)
    pdf.set_font("Helvetica", "B", 10)
    estado_cadena = "INTEGRA Y VERIFICADA" if integridad.get("integro") else "ALERTA DE INTEGRIDAD"
    pdf.cell(0, 7, f" 4. AUDITORIA HASH-CHAIN ({estado_cadena})", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    pdf.set_x(10)
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(0, 5, f"Total de eventos en cadena: {integridad.get('total_registros', 0)} | Ultimo hash: {_limpiar_texto(integridad.get('ultimo_hash', 'N/D'))}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    for log in auditoria[-8:]:
        pdf.set_x(10)
        min_txt = log.get("minuto") if log.get("minuto") is not None else log.get("minuto_partido", "0")
        linea = f"#{log.get('seq')} [Min {min_txt}] {log.get('evento')} - Hash: {str(log.get('hash_actual'))[:24]}..."
        pdf.cell(0, 5, _limpiar_texto(linea), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())