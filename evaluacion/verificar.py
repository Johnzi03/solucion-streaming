#!/usr/bin/env python3
"""Evaluate a simulator run (JSONL log) against the scenario truth (stdlib only, Python 3.9+).

Reports, per pirate stream phase, how fast it was blocked and for what share of users;
legitimate sites hit by blocks; false positives; IP blocks still active after the match;
claims handling; rejected orders; and orders signed by a judge who was not on duty.
"""
import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_DIR = Path(__file__).resolve().parent.parent


def cargar_json(ruta: Path) -> dict:
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def cargar_log(ruta: Path) -> List[dict]:
    with open(ruta, encoding="utf-8") as f:
        eventos = [json.loads(linea) for linea in f if linea.strip()]
    return sorted(eventos, key=lambda e: e["min"])


def reconstruir_ordenes(eventos: List[dict]) -> Dict[Tuple[str, str], dict]:
    """Rebuild each order's lifecycle from the log, keyed by (provider, order id)."""
    ordenes: Dict[Tuple[str, str], dict] = {}
    for e in eventos:
        clave = (e.get("proveedor"), e.get("orden_id"))
        if e["evento"] == "orden_recibida":
            ordenes[clave] = dict(e, aplicada=None, fin=None, estado="recibida")
        elif clave in ordenes:
            orden = ordenes[clave]
            if e["evento"] == "orden_aplicada":
                orden.update(aplicada=e["min"], estado="aplicada")
            elif e["evento"] == "orden_fallida":
                orden.update(estado="fallida")
            elif e["evento"] in ("orden_vencida", "orden_levantada"):
                orden.update(fin=e["min"], estado=e["evento"].replace("orden_", ""))
    return ordenes


def intervalo(orden: dict, desde: float, hasta: float) -> float:
    """Minutes the order was active inside [desde, hasta]."""
    if orden["aplicada"] is None:
        return 0.0
    inicio = max(orden["aplicada"], desde)
    fin = min(orden["fin"] if orden["fin"] is not None else hasta, hasta)
    return max(0.0, fin - inicio)


def activa_en(orden: dict, minuto: float) -> bool:
    return (orden["aplicada"] is not None and orden["aplicada"] <= minuto
            and (orden["fin"] is None or orden["fin"] > minuto))


def afecta(orden: dict, ip: str, host: str) -> bool:
    return (orden["tipo"] == "ip" and orden["objetivo"] == ip) or (orden["tipo"] == "dominio" and orden["objetivo"] == host)


def evaluar(eventos: List[dict], escenario: dict, verdad: dict, proveedores: dict) -> dict:
    ordenes = reconstruir_ordenes(eventos)
    aplicadas = [o for o in ordenes.values() if o["aplicada"] is not None]
    clientes = {p["id"]: p["clientes"] for p in proveedores["proveedores"]}
    total_clientes = sum(clientes.values())
    log_fin = eventos[-1]["min"] if eventos else 0.0
    fin_partido = float(verdad["fin_min"])
    corte = fin_partido + verdad["gracia_min"]
    dns = escenario["dns_pasivo"]
    piratas = set(verdad["dominios_piratas"])
    legitimos = {d for doms in dns.values() for d in doms} - piratas

    # 1. Pirate stream phases: time to first confirmed block, coverage and residual visibility.
    fases = []
    for stream in verdad["streams"]:
        if stream["clase"] != "pirata":
            continue
        for n, fase in enumerate(stream["fases"], start=1):
            desde, hasta = fase["desde_min"], min(fase["hasta_min"], log_fin)
            if desde >= log_fin:
                continue  # The run ended before this phase started.
            coinciden = [o for o in aplicadas if afecta(o, fase["ip"], fase["host"]) and intervalo(o, desde, hasta) > 0]
            bloqueado_min: Dict[str, float] = {}
            for o in coinciden:
                bloqueado_min[o["proveedor"]] = bloqueado_min.get(o["proveedor"], 0.0) + intervalo(o, desde, hasta)
            duracion = max(hasta - desde, 1e-9)
            minutos_cubiertos = sum(clientes.get(p, 0) * min(m, duracion) for p, m in bloqueado_min.items())
            fases.append({
                "stream": stream["id"], "fase": n, "ip": fase["ip"], "host": fase["host"],
                "desde_min": desde, "hasta_min": fase["hasta_min"],
                "bloqueo_recomendado": fase.get("bloqueo_recomendado"),
                "primer_bloqueo_min": round(min(max(o["aplicada"], desde) for o in coinciden) - desde, 2)
                if coinciden else None,
                "proveedores_que_bloquearon": sorted(bloqueado_min),
                "clientes_cubiertos_pct": round(100 * sum(clientes.get(p, 0) for p in bloqueado_min) / total_clientes, 1),
                "visibilidad_residual_pct": round(100 * (1 - minutos_cubiertos / (total_clientes * duracion)), 1),
            })

    # 2. Collateral damage: legitimate domains that sat behind an applied block.
    colateral = []
    for o in aplicadas:
        afectados = dns.get(o["objetivo"], []) if o["tipo"] == "ip" else [o["objetivo"]]
        legit = sorted(d for d in afectados if d in legitimos)
        if legit:
            fin = o["fin"] if o["fin"] is not None else log_fin
            colateral.append({"proveedor": o["proveedor"], "orden_id": o["orden_id"], "tipo": o["tipo"],
                              "objetivo": o["objetivo"], "dominios_legitimos": legit,
                              "minutos_activo": round(max(0.0, fin - o["aplicada"]), 2)})

    # 3. False positives: blocks aimed at the licensed rebroadcaster or at a stream that is not ours.
    falsos = []
    for stream in verdad["streams"]:
        if stream["clase"] == "pirata":
            continue
        for fase in stream["fases"]:
            for o in aplicadas:
                if afecta(o, fase["ip"], fase["host"]):
                    falsos.append({"stream": stream["id"], "clase": stream["clase"], "proveedor": o["proveedor"],
                                   "orden_id": o["orden_id"], "tipo": o["tipo"], "objetivo": o["objetivo"]})

    # 4. Blocks still active after the match (plus grace period).
    post_partido: Optional[dict] = None
    if log_fin >= corte:
        activas = [o for o in aplicadas if activa_en(o, corte)]
        post_partido = {
            "ip": [{"proveedor": o["proveedor"], "orden_id": o["orden_id"], "objetivo": o["objetivo"]}
                   for o in activas if o["tipo"] == "ip"],
            "dominio": [{"proveedor": o["proveedor"], "orden_id": o["orden_id"], "objetivo": o["objetivo"]}
                        for o in activas if o["tipo"] == "dominio"],
        }

    # 5. Claims: was the offending order lifted, did it only expire, or is it still active?
    reclamos = []
    for e in eventos:
        if e["evento"] != "reclamo_emitido":
            continue
        orden = ordenes.get((e["proveedor"], e["orden_id"]))
        resultado, demora = "sin resolver", None
        if orden and orden["fin"] is not None and orden["fin"] >= e["min"]:
            resultado = "levantada" if orden["estado"] == "levantada" else "venció sola"
            demora = round(orden["fin"] - e["min"], 2)
        reclamos.append({"id": e["id"], "dominio": e["dominio"], "proveedor": e["proveedor"],
                         "minuto": e["min"], "resultado": resultado, "minutos_hasta_resolver": demora})

    rechazadas = Counter(e["motivo"] for e in eventos if e["evento"] == "orden_rechazada")
    fallidas = sum(1 for o in ordenes.values() if o["estado"] == "fallida")
    fuera_de_turno = [{"proveedor": o["proveedor"], "orden_id": o["orden_id"], "juez_id": o["juez_id"], "min": o["min"]}
                      for o in ordenes.values() if not o.get("juez_de_turno", True)]

    bloqueadas = [f for f in fases if f["primer_bloqueo_min"] is not None]
    return {
        "escenario": verdad["escenario_id"],
        "log_hasta_min": log_fin,
        "resumen": {
            "fases_piratas_bloqueadas": f"{len(bloqueadas)} de {len(fases)}",
            "visibilidad_residual_promedio_pct": round(sum(f["visibilidad_residual_pct"] for f in fases) / len(fases), 1)
            if fases else None,
            "ordenes_recibidas": len(ordenes),
            "ordenes_aplicadas": len(aplicadas),
            "ordenes_fallidas": fallidas,
            "ordenes_rechazadas": sum(rechazadas.values()),
            "bloqueos_con_dano_colateral": len(colateral),
            "dominios_legitimos_afectados": len({d for c in colateral for d in c["dominios_legitimos"]}),
            "bloqueos_falsos_positivos": len(falsos),
            "bloqueos_ip_activos_post_partido": len(post_partido["ip"]) if post_partido else None,
            "reclamos": len(reclamos),
            "reclamos_resueltos_por_levantamiento": sum(1 for r in reclamos if r["resultado"] == "levantada"),
            "ordenes_firmadas_fuera_de_turno": len(fuera_de_turno),
        },
        "fases": fases,
        "dano_colateral": colateral,
        "falsos_positivos": falsos,
        "post_partido": post_partido,
        "corte_post_partido_min": corte,
        "reclamos": reclamos,
        "rechazos_por_motivo": dict(rechazadas),
        "fuera_de_turno": fuera_de_turno,
    }


def imprimir(r: dict) -> None:
    print(f"Evaluación del escenario {r['escenario']} (registro hasta el minuto {r['log_hasta_min']:.1f})\n")
    print("Resumen")
    for clave, valor in r["resumen"].items():
        print(f"  {clave.replace('_', ' ')}: {valor if valor is not None else 'no evaluable'}")

    print("\n1. Streams piratas por fase")
    for f in r["fases"]:
        primer = f"{f['primer_bloqueo_min']:.1f} min" if f["primer_bloqueo_min"] is not None else "nunca"
        print(f"  {f['stream']} fase {f['fase']} ({f['ip']} / {f['host']}, min {f['desde_min']:g}–{f['hasta_min']:g}): "
              f"primer bloqueo {primer} · clientes cubiertos {f['clientes_cubiertos_pct']} % · "
              f"visibilidad residual {f['visibilidad_residual_pct']} %")

    print("\n2. Daño colateral")
    if not r["dano_colateral"]:
        print("  Ningún bloqueo afectó sitios legítimos.")
    for c in r["dano_colateral"]:
        print(f"  {c['proveedor']} bloqueó {c['tipo']} {c['objetivo']} durante {c['minutos_activo']:.1f} min: "
              f"{len(c['dominios_legitimos'])} sitio(s) legítimo(s) — {', '.join(c['dominios_legitimos'])}")

    print("\n3. Falsos positivos bloqueados")
    if not r["falsos_positivos"]:
        print("  Ninguno.")
    for fp in r["falsos_positivos"]:
        print(f"  {fp['proveedor']} bloqueó {fp['tipo']} {fp['objetivo']} ({fp['stream']}: {fp['clase']})")

    print(f"\n4. Bloqueos activos después del partido (minuto {r['corte_post_partido_min']:g})")
    if r["post_partido"] is None:
        print("  No evaluable: el registro termina antes del cierre. Dejar correr el simulador hasta el final.")
    else:
        for tipo in ("ip", "dominio"):
            items = r["post_partido"][tipo]
            nota = "deberían haber vencido" if tipo == "ip" else "informativo"
            print(f"  Por {tipo} ({nota}): {len(items)}" + "".join(
                f"\n    {i['proveedor']}: {i['objetivo']}" for i in items))

    print("\n5. Reclamos de sitios afectados")
    if not r["reclamos"]:
        print("  No hubo reclamos.")
    for rc in r["reclamos"]:
        demora = f" en {rc['minutos_hasta_resolver']:.1f} min" if rc["minutos_hasta_resolver"] is not None else ""
        print(f"  {rc['id']} {rc['dominio']} ({rc['proveedor']}, min {rc['minuto']:.1f}): {rc['resultado']}{demora}")

    print("\n6. Órdenes rechazadas por los proveedores")
    if not r["rechazos_por_motivo"]:
        print("  Ninguna.")
    for motivo, n in r["rechazos_por_motivo"].items():
        print(f"  {n} × {motivo}")

    print("\n7. Órdenes firmadas por un juez fuera de turno")
    if not r["fuera_de_turno"]:
        print("  Ninguna.")
    for o in r["fuera_de_turno"]:
        print(f"  {o['proveedor']} {o['orden_id']} firmada por {o['juez_id']} en el minuto {o['min']:.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evalúa una corrida del simulador contra la verdad del escenario")
    parser.add_argument("--log", type=Path, default=Path("salida") / "registro.jsonl")
    parser.add_argument("--escenario", type=Path, default=REPO_DIR / "datos" / "escenario_practica.json")
    parser.add_argument("--verdad", type=Path, default=REPO_DIR / "evaluacion" / "verdad_practica.json")
    parser.add_argument("--datos", type=Path, default=REPO_DIR / "datos")
    parser.add_argument("--json", action="store_true", help="imprime el resultado completo en JSON")
    args = parser.parse_args()

    resultado = evaluar(cargar_log(args.log), cargar_json(args.escenario), cargar_json(args.verdad),
                        cargar_json(args.datos / "proveedores.json"))
    if args.json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
    else:
        imprimir(resultado)


if __name__ == "__main__":
    main()