import asyncio
import json
import os
from typing import Any, Dict, List, Optional

import httpx

from app.db import get_conn, inicializar_db, registrar_auditoria, sha256_canonico

SIMULADOR_URL = os.getenv("SIMULADOR_URL", "http://localhost:8080").rstrip("/")


def dominio_raiz(host: str) -> str:
    """Extrae el dominio registrable base (ej. 's1-respaldo.pelotaya.test' -> 'pelotaya.test')."""
    partes = host.lower().strip().rstrip(".").split(".")
    return ".".join(partes[-2:]) if len(partes) >= 2 else host.lower().strip()


class MotorIngesta:
    def __init__(self, simulador_url: str = SIMULADOR_URL) -> None:
        self.simulador_url = simulador_url
        self.partido: Dict[str, Any] = {}
        self.ultimo_min_det: float = -1.0
        self.ultimo_min_rec: float = -1.0

    async def cargar_metadatos_partido(self, client: httpx.AsyncClient) -> Dict[str, Any]:
        resp = await client.get(f"{self.simulador_url}/partido")
        resp.raise_for_status()
        self.partido = resp.json()
        return self.partido

    def _es_marca_valida(self, marca_extraida: str) -> bool:
        """Verifica si la marca extraída comienza con alguno de los prefijos oficiales del partido."""
        marca = marca_extraida.strip()
        if not marca:
            return False

        prefijos: List[str] = []
        for item in self.partido.get("marcas_de_agua_validas", []):
            if isinstance(item, dict) and "prefijo" in item:
                prefijos.append(str(item["prefijo"]))
            elif isinstance(item, str):
                prefijos.append(item)

        if not prefijos:
            return True
        return any(marca.startswith(p) for p in prefijos)

    def _es_licenciatario(self, host: str) -> bool:
        """Verifica si el host o su dominio raíz pertenece a los dominios_autorizados."""
        host_norm = host.lower().strip()
        dominios_autorizados: List[str] = []

        for lic in self.partido.get("licenciatarios", []):
            if isinstance(lic, str):
                dominios_autorizados.append(lic)
            elif isinstance(lic, dict):
                for k in ("dominios_autorizados", "dominios", "hosts"):
                    if isinstance(lic.get(k), list):
                        dominios_autorizados.extend(str(d) for d in lic[k])
                for k in ("dominio", "host"):
                    if isinstance(lic.get(k), str):
                        dominios_autorizados.append(lic[k])

        for dom_raw in dominios_autorizados:
            dom = dom_raw.lower().strip()
            if dom and (host_norm == dom or host_norm.endswith(f".{dom}")):
                return True
        return False

    def _evaluar_riesgo_colateral(self, host: str, dns_info: Dict[str, Any]) -> bool:
        """Devuelve True si la IP aloja múltiples dominios raíz distintos o pertenece a una CDN/hosting compartido."""
        org = str(dns_info.get("organizacion", "")).lower()
        if any(p in org for p in ("cdn", "compartid", "shared", "cloud", "fastly", "akamai", "cloudflare", "edge")):
            return True

        dominios = [d for d in dns_info.get("dominios", []) if d]
        raices = {dominio_raiz(d) for d in dominios}
        raices.add(dominio_raiz(host))

        # Si hay más de 1 dominio raíz distinto en la misma IP -> riesgo colateral -> bloquear por dominio
        return len(raices) > 1

    async def procesar_deteccion(self, client: httpx.AsyncClient, det: Dict[str, Any]) -> Optional[str]:
        if not self.partido:
            await self.cargar_metadatos_partido(client)

        det_id = det["id"]
        minuto = float(det["minuto"])
        host = det["host_stream"]
        ip = det["ip_servidor"]
        marca = str(det.get("marca_de_agua_extraida", ""))

        r_dns = await client.get(f"{self.simulador_url}/dns-pasivo", params={"ip": ip})
        if r_dns.status_code == 200:
            dns_info = r_dns.json()
        else:
            dns_info = {"ip": ip, "dominios": [host], "nota": "sin_datos_dns_pasivo"}

        es_oficial = self._es_marca_valida(marca)
        es_lic = self._es_licenciatario(host)
        ip_compartida = self._evaluar_riesgo_colateral(host, dns_info)

        if not es_oficial or es_lic:
            tipo_rec = "ninguno"
            objetivo_rec = None
            estado_inicial = "DESCARTADO"
        elif ip_compartida:
            tipo_rec = "dominio"
            objetivo_rec = host
            estado_inicial = "PENDIENTE_FIRMA"
        else:
            tipo_rec = "ip"
            objetivo_rec = ip
            estado_inicial = "PENDIENTE_FIRMA"

        if estado_inicial == "PENDIENTE_FIRMA" and objetivo_rec:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM ordenes_proveedor
                        WHERE objetivo = %s AND estado IN ('recibida', 'aplicada') AND vence_min > %s
                        LIMIT 1
                        """,
                        (objetivo_rec, minuto),
                    )
                    if cur.fetchone():
                        tipo_rec = "ninguno"
                        estado_inicial = "YA_BLOQUEADO"

        exp_id = f"EXP-{det_id}"
        evidencia_canonica = {
            "expediente_id": exp_id,
            "deteccion": det,
            "dns_pasivo": dns_info,
            "es_senial_oficial": es_oficial,
            "es_licenciatario": es_lic,
            "ip_compartida": ip_compartida,
            "tipo_bloqueo_recomendado": tipo_rec,
            "objetivo_recomendado": objetivo_rec,
        }
        hash_evidencia = sha256_canonico(evidencia_canonica)

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM expedientes WHERE deteccion_id = %s", (det_id,))
                if cur.fetchone():
                    return None

                cur.execute(
                    """
                    INSERT INTO expedientes (
                        id, deteccion_id, minuto_deteccion, host_stream, ip_servidor,
                        marca_agua_extraida, es_senial_oficial, es_licenciatario,
                        dns_pasivo_json, ip_compartida, tipo_bloqueo_recomendado,
                        objetivo_recomendado, hash_evidencia, estado
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
                    """,
                    (
                        exp_id, det_id, minuto, host, ip, marca, es_oficial, es_lic,
                        json.dumps(dns_info, ensure_ascii=False), ip_compartida,
                        tipo_rec, objetivo_rec, hash_evidencia, estado_inicial,
                    ),
                )
                registrar_auditoria(
                    conn,
                    minuto,
                    "EXPEDIENTE_CREADO",
                    exp_id,
                    "motor_ingesta",
                    {
                        "host": host,
                        "ip": ip,
                        "estado": estado_inicial,
                        "tipo_recomendado": tipo_rec,
                        "objetivo": objetivo_rec,
                        "hash_evidencia": hash_evidencia,
                    },
                )
            conn.commit()
        return exp_id

    async def consultar_nuevas_detecciones(self, client: httpx.AsyncClient) -> List[str]:
        resp = await client.get(f"{self.simulador_url}/detecciones", params={"desde": self.ultimo_min_det})
        resp.raise_for_status()
        lote = resp.json()
        self.ultimo_min_det = float(lote["minuto_actual"])

        creados = []
        for det in lote.get("detecciones", []):
            exp_id = await self.procesar_deteccion(client, det)
            if exp_id:
                creados.append(exp_id)
        return creados

    async def consultar_nuevos_reclamos(self, client: httpx.AsyncClient) -> List[str]:
        resp = await client.get(f"{self.simulador_url}/reclamos", params={"desde": self.ultimo_min_rec})
        resp.raise_for_status()
        lote = resp.json()
        self.ultimo_min_rec = float(lote["minuto_actual"])

        nuevos = []
        for rec in lote.get("reclamos", []):
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM reclamos WHERE id = %s", (rec["id"],))
                    if cur.fetchone():
                        continue

                    cur.execute(
                        """
                        SELECT orden_id FROM ordenes_proveedor
                        WHERE proveedor_id = %s AND estado IN ('recibida', 'aplicada')
                        ORDER BY recibida_min DESC LIMIT 1
                        """,
                        (rec["proveedor"],),
                    )
                    fila_orden = cur.fetchone()
                    orden_afectada = fila_orden["orden_id"] if fila_orden else None

                    cur.execute(
                        """
                        INSERT INTO reclamos (id, minuto_reclamo, dominio, proveedor_id, mensaje, orden_afectada_id)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        """,
                        (rec["id"], rec["minuto"], rec["dominio"], rec["proveedor"], rec.get("mensaje"), orden_afectada),
                    )
                    registrar_auditoria(
                        conn,
                        float(rec["minuto"]),
                        "RECLAMO_RECIBIDO",
                        rec["id"],
                        f"sitio:{rec['dominio']}",
                        {"proveedor": rec["proveedor"], "orden_afectada": orden_afectada},
                    )
                conn.commit()
            nuevos.append(rec["id"])
        return nuevos


async def _prueba_modulo_2() -> None:
    inicializar_db(limpiar_datos=True)
    ingesta = MotorIngesta()
    async with httpx.AsyncClient(timeout=5.0) as client:
        partido = await ingesta.cargar_metadatos_partido(client)
        print(f"Conectado al partido: {partido['nombre']} (fin_min={partido['fin_min']})")
        for _ in range(5):
            nuevos = await ingesta.consultar_nuevas_detecciones(client)
            if nuevos:
                print(f"  [Min {ingesta.ultimo_min_det}] Expedientes creados: {len(nuevos)}")
            await asyncio.sleep(1.0)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, host_stream, ip_compartida, tipo_bloqueo_recomendado, estado FROM expedientes ORDER BY minuto_deteccion LIMIT 15")
            for row in cur.fetchall():
                print(" ", dict(row))


if __name__ == "__main__":
    asyncio.run(_prueba_modulo_2())