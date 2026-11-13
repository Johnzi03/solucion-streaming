import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.reportes import render_pdf
from app.db import sha256_canonico


class ActasUnitTests(unittest.TestCase):
    def test_hash_de_snapshot_cambia_si_se_altera_un_ticket(self):
        snapshot = {"jornada_id": "j-1", "expedientes": [{"id": "EXP-1"}], "ordenes": [], "reclamos": []}
        original = sha256_canonico(snapshot)
        snapshot["expedientes"][0]["id"] = "EXP-ALTERADO"
        self.assertNotEqual(original, sha256_canonico(snapshot))

    def test_pdf_generado_y_hash_corresponde_al_archivo(self):
        snapshot = {
            "jornada_id": "j-1", "zona_horaria": "America/Argentina/Jujuy",
            "generado_en": "2026-11-13T12:00:00-03:00", "minuto_simulado": 105,
            "estadisticas": {"expedientes": 1, "ordenes": 1, "reclamos": 0},
            "expedientes": [{"id": "EXP-1", "minuto_deteccion": 2, "host_stream": "demo.test", "estado": "APROBADO"}],
            "ordenes": [{"orden_id": "ORD-1", "proveedor_id": "isp-1", "objetivo": "203.0.113.1", "estado": "aplicada"}],
            "reclamos": [], "auditoria": [{"seq": 1, "evento": "EXPEDIENTE_CREADO", "entidad_id": "EXP-1", "hash_actual": "a" * 64}],
        }
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "acta.pdf"
            render_pdf(snapshot, "acta-1", sha256_canonico(snapshot), pdf)
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
            self.assertEqual(hashlib.sha256(pdf.read_bytes()).hexdigest(), hashlib.sha256(pdf.read_bytes()).hexdigest())

    def test_pdf_se_genera_para_la_jornada_del_snapshot(self):
        snapshot = {"jornada_id": "jornada-seleccionada", "zona_horaria": "America/Argentina/Jujuy", "generado_en": "ahora", "minuto_simulado": 1, "estadisticas": {}, "expedientes": [], "ordenes": [], "reclamos": [], "auditoria": []}
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "acta.pdf"
            render_pdf(snapshot, "acta-1", sha256_canonico(snapshot), pdf)
            self.assertGreater(pdf.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
