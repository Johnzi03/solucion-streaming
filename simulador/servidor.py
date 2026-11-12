#!/usr/bin/env python3
"""Live blocking simulator for the hackathon football challenge (stdlib only, Python 3.9+).

Serves a simulated match: detections released as simulated time passes, a registry of
fictitious internet providers that receive, apply and expire blocking orders, a passive
DNS lookup and a feed of claims from legitimate sites hit by a block. Every relevant event
is written to a JSONL log that evaluacion/verificar.py reads.
"""
import argparse
import hashlib
import hmac
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

REPO_DIR = Path(__file__).resolve().parent.parent
GRACIA_MIN = 5
CAMPOS_ORDEN = ("orden_id", "expediente_id", "tipo", "objetivo", "vence_min", "juzgado_id", "juez_id", "firma")


def cargar_json(ruta: Path) -> Any:
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def firmar(clave: str, mensaje: str) -> str:
    return hmac.new(clave.encode("utf-8"), mensaje.encode("utf-8"), hashlib.sha256).hexdigest()


def mensaje_orden(orden: dict) -> str:
    return f"{orden['orden_id']}|{orden['tipo']}|{orden['objetivo']}|{orden['vence_min']}|{orden['juez_id']}"


def fraccion(*partes: str) -> float:
    """Deterministic pseudo-random number in [0, 1) derived from the given strings."""
    digest = hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0x100000000


class Simulacion:
    """Holds the match clock, the providers' orders and the claims, guarded by one lock."""

    def __init__(self, escenario: dict, proveedores: dict, juzgados: dict, acelerar: float,
                 inicio_min: float, ruta_log: Path):
        self.lock = threading.RLock()
        self.escenario = escenario
        self.partido = escenario["partido"]
        self.fin_min = float(self.partido["fin_min"])
        self.detecciones = sorted(escenario["detecciones"], key=lambda d: d["minuto"])
        self.dns_pasivo: Dict[str, List[str]] = escenario["dns_pasivo"]
        self.reclamos_auto = {r["dominio"]: r for r in escenario.get("reclamos_automaticos", [])}
        self.proveedores = {p["id"]: p for p in proveedores["proveedores"]}
        self.jueces: Dict[str, Tuple[dict, dict]] = {}
        for juzgado in juzgados["juzgados"]:
            for juez in juzgado["jueces"]:
                self.jueces[juez["id"]] = (juzgado, juez)
        self.turnos = juzgados["turnos"]
        self.acelerar = acelerar
        self.inicio_min = inicio_min
        self.t0 = time.monotonic()
        self.ordenes: Dict[str, Dict[str, dict]] = {pid: {} for pid in self.proveedores}
        self.reclamos: List[dict] = []
        self.reclamos_pendientes: List[dict] = []
        self.hitos = set()
        ruta_log.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(ruta_log, "w", encoding="utf-8")
        self.registrar("simulacion_iniciada", self.ahora(), escenario=escenario["id"],
                       acelerar=acelerar, fin_min=self.fin_min)

    # --- clock and log -------------------------------------------------------------

    def ahora(self) -> float:
        return self.inicio_min + (time.monotonic() - self.t0) * self.acelerar / 60.0

    def registrar(self, evento: str, minuto: float, **datos: Any) -> None:
        with self.lock:
            linea = {"min": round(minuto, 3), "evento": evento}
            linea.update(datos)
            self.log.write(json.dumps(linea, ensure_ascii=False) + "\n")
            self.log.flush()

    # --- read-only views -------------------------------------------------------------

    def estado(self) -> dict:
        ahora = self.ahora()
        return {
            "partido": self.partido["id"],
            "minuto_actual": round(ahora, 2),
            "fin_min": self.fin_min,
            "finalizado": ahora >= self.fin_min,
            "acelerar": self.acelerar,
            "jueces_de_turno": self.jueces_de_turno(ahora),
        }

    def detecciones_desde(self, desde: float) -> dict:
        # Filter and answer with the same rounded cutoff, so `desde=minuto_actual` never skips a detection.
        corte = round(self.ahora(), 3)
        nuevas = [d for d in self.detecciones if desde < d["minuto"] <= corte]
        return {"minuto_actual": corte, "detecciones": nuevas}

    def dns(self, ip: Optional[str], dominio: Optional[str]) -> Tuple[int, dict]:
        asn_de_ip = self.escenario["ip_asn"]
        if ip:
            if ip not in self.dns_pasivo:
                return 404, {"error": f"Sin registros de DNS pasivo para la IP {ip}"}
            asn = asn_de_ip.get(ip)
            return 200, {"ip": ip, "asn": asn, "organizacion": self.escenario["asn"].get(str(asn)),
                         "dominios": self.dns_pasivo[ip]}
        if dominio:
            ips = [i for i, doms in self.dns_pasivo.items() if dominio in doms]
            if not ips:
                return 404, {"error": f"Sin registros de DNS pasivo para el dominio {dominio}"}
            return 200, {"dominio": dominio, "ips": [
                {"ip": i, "asn": asn_de_ip.get(i), "organizacion": self.escenario["asn"].get(str(asn_de_ip.get(i)))}
                for i in ips]}
        return 400, {"error": "Indicar ?ip= o ?dominio="}

    def lista_proveedores(self) -> List[dict]:
        # Behaviour (delay, failure rate) stays internal; teams discover it by using the API.
        return [
            dict({k: v for k, v in p.items() if k != "comportamiento"}, ordenes=f"/proveedores/{p['id']}/ordenes")
            for p in self.proveedores.values()
        ]

    def lista_juzgados(self) -> dict:
        # Signing keys are only in datos/juzgados.json, never served by the API.
        juzgados = {}
        for juzgado, _ in self.jueces.values():
            juzgados[juzgado["id"]] = {
                "id": juzgado["id"], "nombre": juzgado["nombre"], "jurisdiccion": juzgado["jurisdiccion"],
                "jueces": [{"id": j["id"], "nombre": j["nombre"]} for j in juzgado["jueces"]],
            }
        return {"juzgados": list(juzgados.values()), "turnos": self.turnos,
                "de_turno_ahora": self.jueces_de_turno(self.ahora())}

    def jueces_de_turno(self, minuto: float) -> List[str]:
        return [t["juez_id"] for t in self.turnos if t["desde_min"] <= minuto < t["hasta_min"]]

    def reclamos_desde(self, desde: float) -> dict:
        with self.lock:
            return {"minuto_actual": round(self.ahora(), 2),
                    "reclamos": [r for r in self.reclamos if r["minuto"] > desde]}

    @staticmethod
    def vista_orden(orden: dict) -> dict:
        return {k: v for k, v in orden.items() if not k.startswith("_")}

    # --- orders ------------------------------------------------------------------------

    def _validar(self, proveedor: dict, cuerpo: dict, ahora: float) -> Optional[Tuple[int, str]]:
        faltantes = [c for c in CAMPOS_ORDEN if c not in cuerpo]
        if faltantes:
            return 400, f"Faltan campos: {', '.join(faltantes)}"
        if cuerpo["orden_id"] in self.ordenes[proveedor["id"]]:
            return 409, "Ya existe una orden con ese orden_id en este proveedor"
        if cuerpo["tipo"] not in ("ip", "dominio"):
            return 422, "El tipo debe ser 'ip' o 'dominio'"
        if cuerpo["tipo"] not in proveedor["capacidades"]:
            return 422, f"{proveedor['nombre']} no aplica bloqueos por {cuerpo['tipo']}"
        vence = cuerpo["vence_min"]
        if not isinstance(vence, int) or isinstance(vence, bool):
            return 422, "vence_min debe ser un número entero de minutos de partido"
        if vence <= ahora:
            return 422, "vence_min debe ser posterior al minuto actual"
        registro = self.jueces.get(cuerpo["juez_id"])
        if registro is None:
            return 403, "El juez no figura en el registro de juzgados"
        if registro[0]["id"] != cuerpo["juzgado_id"]:
            return 403, "El juez no pertenece al juzgado indicado"
        esperada = firmar(registro[1]["clave_firma_demo"], mensaje_orden(cuerpo))
        if not hmac.compare_digest(esperada, str(cuerpo["firma"])):
            return 403, "Firma inválida"
        return None

    def recibir_orden(self, pid: str, cuerpo: dict) -> Tuple[int, dict]:
        with self.lock:
            proveedor = self.proveedores.get(pid)
            if proveedor is None:
                return 404, {"error": f"Proveedor desconocido: {pid}"}
            ahora = self.ahora()
            error = self._validar(proveedor, cuerpo, ahora)
            if error:
                self.registrar("orden_rechazada", ahora, proveedor=pid, orden_id=cuerpo.get("orden_id"),
                               motivo=error[1])
                return error[0], {"error": error[1]}
            comportamiento = proveedor["comportamiento"]
            demora = comportamiento["demora_min"] * (0.7 + 0.6 * fraccion(pid, cuerpo["orden_id"], "demora"))
            orden = {c: cuerpo[c] for c in CAMPOS_ORDEN if c != "firma"}
            orden.update({
                "estado": "recibida",
                "recibida_min": round(ahora, 3),
                "_aplica_min": ahora + demora,
                "_falla": fraccion(pid, cuerpo["orden_id"], "falla") < comportamiento["tasa_falla"],
            })
            self.ordenes[pid][cuerpo["orden_id"]] = orden
            de_turno = cuerpo["juez_id"] in self.jueces_de_turno(ahora)
            self.registrar("orden_recibida", ahora, proveedor=pid, juez_de_turno=de_turno,
                           **{k: orden[k] for k in CAMPOS_ORDEN if k != "firma"})
            return 202, {"orden_id": orden["orden_id"], "estado": "recibida",
                         "consultar": f"/proveedores/{pid}/ordenes/{orden['orden_id']}"}

    def levantar_orden(self, pid: str, orden_id: str, cuerpo: dict) -> Tuple[int, dict]:
        with self.lock:
            orden = self.ordenes.get(pid, {}).get(orden_id)
            if orden is None:
                return 404, {"error": "Orden inexistente"}
            registro = self.jueces.get(str(cuerpo.get("juez_id")))
            if registro is None:
                return 403, {"error": "El juez no figura en el registro de juzgados"}
            esperada = firmar(registro[1]["clave_firma_demo"], f"LEVANTAR|{orden_id}|{cuerpo['juez_id']}")
            if not hmac.compare_digest(esperada, str(cuerpo.get("firma"))):
                return 403, {"error": "Firma inválida"}
            if orden["estado"] not in ("recibida", "aplicada"):
                return 409, {"error": f"La orden ya no está activa (estado: {orden['estado']})"}
            ahora = self.ahora()
            orden["estado"] = "levantada"
            orden["finalizada_min"] = round(ahora, 3)
            self.registrar("orden_levantada", ahora, proveedor=pid, orden_id=orden_id,
                           juez_id=cuerpo["juez_id"], motivo=cuerpo.get("motivo"))
            return 200, self.vista_orden(orden)

    def ver_orden(self, pid: str, orden_id: Optional[str]) -> Tuple[int, Any]:
        with self.lock:
            if pid not in self.ordenes:
                return 404, {"error": f"Proveedor desconocido: {pid}"}
            if orden_id is None:
                return 200, [self.vista_orden(o) for o in self.ordenes[pid].values()]
            orden = self.ordenes[pid].get(orden_id)
            if orden is None:
                return 404, {"error": "Orden inexistente"}
            return 200, self.vista_orden(orden)

    # --- background progress -------------------------------------------------------

    def avanzar(self) -> None:
        with self.lock:
            ahora = self.ahora()
            for pid, ordenes in self.ordenes.items():
                for orden in ordenes.values():
                    self._avanzar_orden(pid, orden, ahora)
            self._emitir_reclamos(ahora)
            if ahora >= self.fin_min and "fin" not in self.hitos:
                self.hitos.add("fin")
                self.registrar("partido_finalizado", self.fin_min)
            if ahora >= self.fin_min + GRACIA_MIN and "cierre" not in self.hitos:
                self.hitos.add("cierre")
                activas = [{"proveedor": pid, "orden_id": o["orden_id"], "tipo": o["tipo"], "objetivo": o["objetivo"]}
                           for pid, ords in self.ordenes.items() for o in ords.values() if o["estado"] == "aplicada"]
                self.registrar("ventana_cerrada", self.fin_min + GRACIA_MIN, ordenes_activas=activas)

    def _avanzar_orden(self, pid: str, orden: dict, ahora: float) -> None:
        if orden["estado"] == "recibida" and ahora >= orden["_aplica_min"]:
            minuto = orden["_aplica_min"]
            if orden["_falla"]:
                orden["estado"] = "fallida"
                orden["motivo"] = "Error interno del proveedor. Reenviar la orden con un nuevo orden_id."
                self.registrar("orden_fallida", minuto, proveedor=pid, orden_id=orden["orden_id"],
                               motivo=orden["motivo"])
                return
            orden["estado"] = "aplicada"
            orden["aplicada_min"] = round(minuto, 3)
            self.registrar("orden_aplicada", minuto, proveedor=pid, orden_id=orden["orden_id"],
                           tipo=orden["tipo"], objetivo=orden["objetivo"])
            self._programar_reclamos(pid, orden)
        if orden["estado"] == "aplicada" and ahora >= orden["vence_min"]:
            minuto = max(float(orden["vence_min"]), orden["aplicada_min"])
            orden["estado"] = "vencida"
            orden["finalizada_min"] = round(minuto, 3)
            self.registrar("orden_vencida", minuto, proveedor=pid, orden_id=orden["orden_id"])

    def _programar_reclamos(self, pid: str, orden: dict) -> None:
        afectados = self.dns_pasivo.get(orden["objetivo"], []) if orden["tipo"] == "ip" else [orden["objetivo"]]
        for dominio in afectados:
            plantilla = self.reclamos_auto.get(dominio)
            if plantilla:
                self.reclamos_pendientes.append({
                    "minuto": orden["aplicada_min"] + plantilla["demora_min"], "dominio": dominio,
                    "proveedor": pid, "orden_id": orden["orden_id"], "mensaje": plantilla["mensaje"],
                })

    def _emitir_reclamos(self, ahora: float) -> None:
        pendientes = []
        for p in self.reclamos_pendientes:
            if p["minuto"] > ahora:
                pendientes.append(p)
                continue
            orden = self.ordenes[p["proveedor"]][p["orden_id"]]
            sigue_activa = orden["estado"] == "aplicada" or orden.get("finalizada_min", 0) > p["minuto"]
            if not sigue_activa:
                continue
            reclamo = {
                "id": f"R-{len(self.reclamos) + 1:03d}", "minuto": round(p["minuto"], 3), "dominio": p["dominio"],
                "proveedor": p["proveedor"], "mensaje": p["mensaje"],
            }
            self.reclamos.append(reclamo)
            self.registrar("reclamo_emitido", p["minuto"], orden_id=p["orden_id"],
                           **{k: v for k, v in reclamo.items() if k != "minuto"})
        self.reclamos_pendientes = pendientes


INDICE = {
    "simulador": "Simulador de bloqueo en vivo — Desafío Fútbol, Hackathon Argentina Space 2026",
    "aviso": "Todos los datos son ficticios. No se bloquea nada real.",
    "endpoints": {
        "GET /estado": "Minuto simulado actual y jueces de turno",
        "GET /partido": "Partido, marcas de agua válidas y licenciatarios",
        "GET /detecciones?desde=<min>": "Detecciones de streams publicadas después del minuto indicado",
        "GET /dns-pasivo?ip=<ip> | ?dominio=<dominio>": "Dominios vistos en una IP, o IPs de un dominio",
        "GET /juzgados": "Juzgados, jueces y turnos (sin claves)",
        "GET /proveedores": "Registro de proveedores de internet",
        "POST /proveedores/<id>/ordenes": "Enviar una orden de bloqueo firmada",
        "GET /proveedores/<id>/ordenes[/<orden_id>]": "Estado de las órdenes",
        "POST /proveedores/<id>/ordenes/<orden_id>/levantar": "Levantar una orden (firmada por un juez)",
        "GET /reclamos?desde=<min>": "Reclamos de sitios afectados por un bloqueo",
    },
}


class Manejador(BaseHTTPRequestHandler):
    sim: Simulacion

    def log_message(self, formato: str, *args: Any) -> None:
        pass  # Keep the console clean; the JSONL log is the source of truth.

    def _responder(self, estado: int, datos: Any) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(estado)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _ruta(self) -> Tuple[List[str], Dict[str, str]]:
        url = urlparse(self.path)
        consulta = {k: v[0] for k, v in parse_qs(url.query).items()}
        return [p for p in url.path.split("/") if p], consulta

    def _numero(self, consulta: Dict[str, str], nombre: str) -> float:
        try:
            return float(consulta.get(nombre, "-1e9"))
        except ValueError:
            return -1e9

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        partes, consulta = self._ruta()
        sim = self.sim
        if not partes:
            return self._responder(200, INDICE)
        if partes == ["estado"]:
            return self._responder(200, sim.estado())
        if partes == ["partido"]:
            return self._responder(200, sim.partido)
        if partes == ["detecciones"]:
            return self._responder(200, sim.detecciones_desde(self._numero(consulta, "desde")))
        if partes == ["dns-pasivo"]:
            return self._responder(*sim.dns(consulta.get("ip"), consulta.get("dominio")))
        if partes == ["juzgados"]:
            return self._responder(200, sim.lista_juzgados())
        if partes == ["proveedores"]:
            return self._responder(200, sim.lista_proveedores())
        if partes == ["reclamos"]:
            return self._responder(200, sim.reclamos_desde(self._numero(consulta, "desde")))
        if len(partes) in (3, 4) and partes[0] == "proveedores" and partes[2] == "ordenes":
            return self._responder(*sim.ver_orden(partes[1], partes[3] if len(partes) == 4 else None))
        self._responder(404, {"error": "Ruta inexistente. GET / lista los endpoints."})

    def do_POST(self) -> None:
        partes, _ = self._ruta()
        try:
            largo = int(self.headers.get("Content-Length", "0"))
            cuerpo = json.loads(self.rfile.read(largo) or b"{}")
            if not isinstance(cuerpo, dict):
                raise ValueError
        except ValueError:
            return self._responder(400, {"error": "El cuerpo debe ser un objeto JSON"})
        if len(partes) == 3 and partes[0] == "proveedores" and partes[2] == "ordenes":
            return self._responder(*self.sim.recibir_orden(partes[1], cuerpo))
        if len(partes) == 5 and partes[0] == "proveedores" and partes[2] == "ordenes" and partes[4] == "levantar":
            return self._responder(*self.sim.levantar_orden(partes[1], partes[3], cuerpo))
        self._responder(404, {"error": "Ruta inexistente. GET / lista los endpoints."})


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulador de bloqueo en vivo (Desafío Fútbol)")
    parser.add_argument("--escenario", type=Path, default=REPO_DIR / "datos" / "escenario_practica.json")
    parser.add_argument("--datos", type=Path, default=REPO_DIR / "datos",
                        help="carpeta con proveedores.json y juzgados.json")
    parser.add_argument("--acelerar", type=float, default=1.0,
                        help="minutos simulados por minuto real (6 = el partido dura 17,5 minutos)")
    parser.add_argument("--inicio-min", type=float, default=0.0, help="minuto de partido en el que arranca")
    parser.add_argument("--puerto", type=int, default=8080)
    parser.add_argument("--log", type=Path, default=Path("salida") / "registro.jsonl")
    args = parser.parse_args()

    sim = Simulacion(
        escenario=cargar_json(args.escenario),
        proveedores=cargar_json(args.datos / "proveedores.json"),
        juzgados=cargar_json(args.datos / "juzgados.json"),
        acelerar=args.acelerar,
        inicio_min=args.inicio_min,
        ruta_log=args.log,
    )
    Manejador.sim = sim

    def ciclo() -> None:
        while True:
            try:
                sim.avanzar()
            except Exception:  # Keep the clock alive; a bug here must not freeze the match.
                traceback.print_exc()
            time.sleep(0.1)

    threading.Thread(target=ciclo, daemon=True).start()
    servidor = ThreadingHTTPServer(("0.0.0.0", args.puerto), Manejador)
    duracion_real = sim.fin_min / args.acelerar
    print(f"Simulador en http://localhost:{args.puerto} — {sim.partido['nombre']}", flush=True)
    print(f"{sim.fin_min:.0f} minutos de partido ≈ {duracion_real:.1f} minutos reales (x{args.acelerar:g})", flush=True)
    print(f"Registro de eventos: {args.log}", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nSimulador detenido.")
    finally:
        sim.log.close()


if __name__ == "__main__":
    main()