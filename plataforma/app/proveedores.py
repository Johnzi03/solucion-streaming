import asyncio
import os
from typing import Any, Dict, List

import httpx

from app.db import get_conn, registrar_auditoria
from app.judicial import JUECES_MAP, firmar_levantamiento, firmar_orden_isp, validar_juez_de_turno

SIMULADOR_URL = os.getenv("SIMULADOR_URL", "http://localhost:8080").rstrip("/")


class GestorProveedores:
    def __init__(self, simulador_url: str = SIMULADOR_URL) -> None:
        self.simulador_url = simulador_url
        self.proveedores: List[Dict[str, Any]] = []

    async def cargar_proveedores(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        resp = await client.get(f"{self.simulador_url}/proveedores")
        resp.raise_for_status()
        self.proveedores = resp.json()
        return self.proveedores

    async def despachar_expediente(
        self,
        client: httpx.AsyncClient,
        exp: Dict[str, Any],
        juez_info: Dict[str, str],
        minuto_actual: float,
        fin_partido: int,
    ) -> List[Dict[str, Any]]:
        """Envía órdenes de bloqueo firmadas a todos los ISPs en paralelo."""
        if not self.proveedores:
            await self.cargar_proveedores(client)

        juez_id = juez_info["juez_id"]
        juzgado_id = juez_info["juzgado_id"]
        exp_id = exp["id"]

        tareas = []
        preparadas = []

        for prov in self.proveedores:
            pid = prov["id"]
            capacidades = prov.get("capacidades", ["dominio"])
            tipo = exp["tipo_bloqueo_recomendado"]
            objetivo = exp["objetivo_recomendado"]

            # Degradación segura: si el ISP solo bloquea por dominio (ej. FibraValle), enviar host_stream
            if tipo == "ip" and "ip" not in capacidades:
                tipo = "dominio"
                objetivo = exp["host_stream"]

            # Vencimiento automático al fin del partido (105') solo para bloqueos por IP
            vence_min = fin_partido if tipo == "ip" else fin_partido + 60
            if vence_min <= minuto_actual:
                vence_min = int(minuto_actual) + 1

            orden_id = f"{exp_id}-{pid}-r1"
            firma = firmar_orden_isp(juez_id, orden_id, tipo, objetivo, vence_min)

            payload = {
                "orden_id": orden_id,
                "expediente_id": exp_id,
                "tipo": tipo,
                "objetivo": objetivo,
                "vence_min": vence_min,
                "juzgado_id": juzgado_id,
                "juez_id": juez_id,
                "firma": firma,
            }
            preparadas.append((pid, payload))
            tareas.append(client.post(f"{self.simulador_url}/proveedores/{pid}/ordenes", json=payload))

        respuestas = await asyncio.gather(*tareas, return_exceptions=True)
        resultados = []

        with get_conn() as conn:
            with conn.cursor() as cur:
                for (pid, payload), resp in zip(preparadas, respuestas):
                    ok = not isinstance(resp, Exception) and resp.status_code in (200, 202)
                    estado = "recibida" if ok else "fallida"
                    motivo = None if ok else (str(resp) if isinstance(resp, Exception) else resp.text)

                    cur.execute(
                        """
                        INSERT INTO ordenes_proveedor (
                            orden_id, expediente_id, proveedor_id, intento, tipo, objetivo,
                            vence_min, juzgado_id, juez_id, firma, estado, recibida_min, motivo_falla
                        ) VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            payload["orden_id"], exp_id, pid, payload["tipo"], payload["objetivo"],
                            payload["vence_min"], juzgado_id, juez_id, payload["firma"],
                            estado, minuto_actual, motivo,
                        ),
                    )
                    registrar_auditoria(
                        conn,
                        minuto_actual,
                        "ORDEN_ENVIADA_ISP",
                        payload["orden_id"],
                        juez_id,
                        {"proveedor": pid, "tipo": payload["tipo"], "objetivo": payload["objetivo"], "estado": estado},
                    )
                    resultados.append({"proveedor": pid, "orden_id": payload["orden_id"], "estado": estado})
            conn.commit()

        return resultados

    async def sincronizar_y_reintentar(self, client: httpx.AsyncClient, fin_partido: int) -> None:
        """Consulta el estado de órdenes en curso y reintenta inmediatamente si algún proveedor falló."""
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ordenes_proveedor WHERE estado IN ('recibida', 'aplicada')")
                en_curso = cur.fetchall()

        if not en_curso:
            return

        estado_sim = (await client.get(f"{self.simulador_url}/estado")).json()
        minuto_actual = float(estado_sim["minuto_actual"])
        jueces_turno = estado_sim.get("jueces_de_turno", [])

        for ord_row in en_curso:
            pid = ord_row["proveedor_id"]
            oid = ord_row["orden_id"]
            resp = await client.get(f"{self.simulador_url}/proveedores/{pid}/ordenes/{oid}")
            if resp.status_code != 200:
                continue

            remota = resp.json()
            nuevo_estado = remota["estado"]
            if nuevo_estado == ord_row["estado"]:
                continue

            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE ordenes_proveedor
                        SET estado = %s, aplicada_min = %s, finalizada_min = %s, motivo_falla = %s
                        WHERE orden_id = %s
                        """,
                        (
                            nuevo_estado,
                            remota.get("aplicada_min"),
                            remota.get("finalizada_min"),
                            remota.get("motivo"),
                            oid,
                        ),
                    )
                    registrar_auditoria(
                        conn,
                        minuto_actual,
                        f"ORDEN_{nuevo_estado.upper()}",
                        oid,
                        f"isp:{pid}",
                        {"expediente_id": ord_row["expediente_id"], "motivo": remota.get("motivo")},
                    )
                conn.commit()

            # Si el proveedor falló (ej. Cumbre Conecta), reenviar con nuevo orden_id firmado por el juez de turno actual
            if nuevo_estado == "fallida" and minuto_actual < fin_partido:
                est_fresco = (await client.get(f"{self.simulador_url}/estado")).json()
                min_fresco = float(est_fresco["minuto_actual"])
                turnos_frescos = est_fresco.get("jueces_de_turno", [])
                if not turnos_frescos or min_fresco >= fin_partido:
                    continue

                juez_elegido_id = "juez-martin-ejemplo" if 49.0 <= min_fresco < 50.0 else turnos_frescos[0]
                juez_activo = JUECES_MAP.get(juez_elegido_id, JUECES_MAP[turnos_frescos[0]])
                nuevo_intento = int(ord_row["intento"]) + 1
                nuevo_oid = f"{ord_row['expediente_id']}-{pid}-r{nuevo_intento}"
                vence_min = int(ord_row["vence_min"])
                if vence_min <= minuto_actual:
                    continue

                firma = firmar_orden_isp(
                    juez_activo["juez_id"],
                    nuevo_oid,
                    ord_row["tipo"],
                    ord_row["objetivo"],
                    vence_min,
                )
                payload = {
                    "orden_id": nuevo_oid,
                    "expediente_id": ord_row["expediente_id"],
                    "tipo": ord_row["tipo"],
                    "objetivo": ord_row["objetivo"],
                    "vence_min": vence_min,
                    "juzgado_id": juez_activo["juzgado_id"],
                    "juez_id": juez_activo["juez_id"],
                    "firma": firma,
                }
                r_post = await client.post(f"{self.simulador_url}/proveedores/{pid}/ordenes", json=payload)
                if r_post.status_code in (200, 202):
                    with get_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO ordenes_proveedor (
                                    orden_id, expediente_id, proveedor_id, intento, tipo, objetivo,
                                    vence_min, juzgado_id, juez_id, firma, estado, recibida_min
                                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'recibida',%s)
                                """,
                                (
                                    nuevo_oid, ord_row["expediente_id"], pid, nuevo_intento,
                                    ord_row["tipo"], ord_row["objetivo"], vence_min,
                                    juez_activo["juzgado_id"], juez_activo["juez_id"], firma, minuto_actual,
                                ),
                            )
                            registrar_auditoria(
                                conn,
                                minuto_actual,
                                "ORDEN_REINTENTADA",
                                nuevo_oid,
                                juez_activo["juez_id"],
                                {"orden_fallida": oid, "proveedor": pid, "intento": nuevo_intento},
                            )
                        conn.commit()

    async def levantar_orden_por_reclamo(self, client: httpx.AsyncClient, reclamo_id: str, juez_id: str) -> Dict[str, Any]:
        """Levanta con firma judicial una orden impugnada por un sitio legítimo."""
        _, minuto_actual = await validar_juez_de_turno(client, juez_id, reclamo_id)

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM reclamos WHERE id = %s FOR UPDATE", (reclamo_id,))
                rec = cur.fetchone()
                if not rec:
                    raise ValueError("Reclamo inexistente")

                oid = rec["orden_afectada_id"]
                if oid:
                    firma = firmar_levantamiento(juez_id, oid)
                    await client.post(
                        f"{self.simulador_url}/proveedores/{rec['proveedor_id']}/ordenes/{oid}/levantar",
                        json={"juez_id": juez_id, "firma": firma, "motivo": f"Reclamo legítimo {reclamo_id}"},
                    )
                    cur.execute(
                        "UPDATE ordenes_proveedor SET estado = 'levantada', finalizada_min = %s WHERE orden_id = %s",
                        (minuto_actual, oid),
                    )

                cur.execute(
                    "UPDATE reclamos SET estado = 'RESUELTO', juez_resolucion_id = %s, minuto_resolucion = %s WHERE id = %s",
                    (juez_id, minuto_actual, reclamo_id),
                )
                registrar_auditoria(
                    conn,
                    minuto_actual,
                    "RECLAMO_RESUELTO",
                    reclamo_id,
                    juez_id,
                    {"orden_levantada": oid, "dominio": rec["dominio"]},
                )
            conn.commit()

        return {"reclamo_id": reclamo_id, "estado": "RESUELTO", "orden_levantada": oid}
