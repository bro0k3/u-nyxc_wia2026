"""Tes otomatis: python -m unittest -v

Fokus utama: hanya admin yang bisa memverifikasi laporan / mengubah data.
Memakai database sementara, jadi data asli di folder data/ tidak tersentuh.
"""
import io
import json
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ["FIREWATCH_DATA_DIR"] = _tmp  # harus sebelum `import app`

from werkzeug.security import generate_password_hash  # noqa: E402

import database  # noqa: E402
from app import create_app  # noqa: E402

CSRF = {"X-Requested-With": "FireWatch"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class TesFireWatch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = create_app(self.dir)
        conn = database.connect(self.app.config["DB_PATH"])
        conn.execute("INSERT INTO admin (username, password_hash, dibuat_pada) VALUES (?, ?, ?)",
                     ("budi", generate_password_hash("kata-sandi-kuat"), database.sekarang()))
        conn.commit()
        conn.close()
        self.publik = self.app.test_client()
        self.admin = self.app.test_client()
        r = self.admin.post("/api/admin/login", json={"username": "budi", "password": "kata-sandi-kuat"}, headers=CSRF)
        self.assertEqual(r.status_code, 200)

    def kirim_laporan(self, **ubah):
        data = {"jenis": "Titik Api Baru", "lokasi": "Desa Jabiren", "lat": "-2.72", "lng": "114.28",
                "deskripsi": "Asap tebal di area gambut, butuh bantuan."}
        data.update(ubah)
        return self.publik.post("/api/laporan", data=data, content_type="multipart/form-data")

    # ------------------------------------------------------------- data awal
    def test_anggaran_sama_dengan_json_awal(self):
        with open(os.path.join(os.path.dirname(__file__), "seed", "anggaran.json"), encoding="utf-8") as f:
            asli = json.load(f)
        hasil = self.publik.get("/api/anggaran").get_json()
        self.assertEqual(hasil["ringkasan"], asli["ringkasan"])
        for a, b in zip(asli["sektor_penyerapan"], hasil["sektor_penyerapan"]):
            for kunci, nilai in a.items():
                self.assertEqual(b[kunci], nilai, f"{a['id']}.{kunci}")
        self.assertEqual(len(hasil["pos_kas"]["masuk"]), 3)
        self.assertEqual(hasil["pos_kas"]["masuk"][0]["nilai"], "Rp 2.218,17 T")

    # ------------------------------------------------------- hanya admin
    def test_publik_tidak_bisa_memverifikasi(self):
        lid = self.kirim_laporan().get_json()["id"]
        r = self.publik.patch(f"/api/admin/laporan/{lid}/status", json={"status": "terverifikasi"}, headers=CSRF)
        self.assertEqual(r.status_code, 401)
        status = self.publik.get("/api/laporan").get_json()["laporan"][0]["status"]
        self.assertEqual(status, "menunggu")

    def test_semua_endpoint_admin_menolak_publik(self):
        daftar = [
            ("get", "/api/admin/laporan"), ("get", "/api/admin/langganan"),
            ("patch", "/api/admin/laporan/1/status"), ("delete", "/api/admin/laporan/1"),
            ("put", "/api/admin/ringkasan"), ("post", "/api/admin/sektor"),
            ("put", "/api/admin/sektor/pemadaman"), ("delete", "/api/admin/sektor/pemadaman"),
            ("post", "/api/admin/pos-kas"), ("put", "/api/admin/pos-kas/1"),
            ("delete", "/api/admin/pos-kas/1"), ("delete", "/api/admin/langganan/1"),
        ]
        for metode, url in daftar:
            r = getattr(self.publik, metode)(url, json={}, headers=CSRF)
            self.assertEqual(r.status_code, 401, f"{metode} {url}")

    def test_admin_bisa_verifikasi_dan_tercatat(self):
        lid = self.kirim_laporan().get_json()["id"]
        r = self.admin.patch(f"/api/admin/laporan/{lid}/status", json={"status": "terverifikasi"}, headers=CSRF)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["ditinjau_oleh"], "budi")
        publik = self.publik.get("/api/laporan").get_json()
        self.assertEqual(publik["laporan"][0]["status"], "terverifikasi")
        self.assertNotIn("ditinjau_oleh", publik["laporan"][0])  # nama admin tidak bocor ke publik
        self.assertEqual(publik["jumlah"], {"terverifikasi": 1, "menunggu": 0})

    def test_tanpa_header_csrf_ditolak_meski_sudah_login(self):
        lid = self.kirim_laporan().get_json()["id"]
        r = self.admin.patch(f"/api/admin/laporan/{lid}/status", json={"status": "terverifikasi"})
        self.assertEqual(r.status_code, 403)

    def test_logout_mencabut_akses(self):
        self.admin.post("/api/admin/logout", headers=CSRF)
        self.assertEqual(self.admin.get("/api/admin/laporan").status_code, 401)

    def test_login_salah_dan_pembatasan(self):
        c = self.app.test_client()
        for _ in range(5):
            r = c.post("/api/admin/login", json={"username": "budi", "password": "salah"}, headers=CSRF)
            self.assertEqual(r.status_code, 401)
        r = c.post("/api/admin/login", json={"username": "budi", "password": "kata-sandi-kuat"}, headers=CSRF)
        self.assertEqual(r.status_code, 429)

    def test_akun_admin_dihapus_sesi_tidak_berlaku(self):
        conn = database.connect(self.app.config["DB_PATH"])
        conn.execute("DELETE FROM admin")
        conn.commit()
        self.assertEqual(self.admin.get("/api/admin/laporan").status_code, 401)

    # ------------------------------------------------------- laporan publik
    def test_laporan_valid_masuk_status_menunggu(self):
        r = self.kirim_laporan()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["status"], "menunggu")

    def test_validasi_laporan(self):
        self.assertEqual(self.kirim_laporan(jenis="Aneh").status_code, 400)
        self.assertEqual(self.kirim_laporan(deskripsi="pendek").status_code, 400)
        self.assertEqual(self.kirim_laporan(lat="999").status_code, 400)
        self.assertEqual(self.kirim_laporan(lat="abc").status_code, 400)

    def test_status_tidak_bisa_diisi_sendiri_oleh_pelapor(self):
        r = self.kirim_laporan(status="terverifikasi")
        self.assertEqual(r.get_json()["status"], "menunggu")

    def test_laporan_dibatasi_laju(self):
        kode = [self.kirim_laporan().status_code for _ in range(6)]
        self.assertEqual(kode, [201] * 5 + [429])

    def test_foto_hanya_gambar_asli_dan_privat_sebelum_verifikasi(self):
        palsu = self.publik.post("/api/laporan", content_type="multipart/form-data", data={
            "jenis": "Titik Api Baru", "lokasi": "Desa X", "deskripsi": "Deskripsi cukup panjang.",
            "foto": (io.BytesIO(b"<script>alert(1)</script>"), "x.png")})
        self.assertEqual(palsu.status_code, 400)

        r = self.publik.post("/api/laporan", content_type="multipart/form-data", data={
            "jenis": "Titik Api Baru", "lokasi": "Desa X", "deskripsi": "Deskripsi cukup panjang.",
            "foto": (io.BytesIO(PNG), "../../etc/passwd.png")})
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertTrue(body["ada_foto"])
        self.assertIsNone(body["foto_url"])  # belum diverifikasi -> tidak diberi ke publik
        # admin melihat URL-nya
        admin_list = self.admin.get("/api/admin/laporan").get_json()["laporan"][0]
        url = admin_list["foto_url"]
        self.assertTrue(url.endswith(".png") and ".." not in url)
        self.assertEqual(self.publik.get(url).status_code, 404)
        self.assertEqual(self.admin.get(url).status_code, 200)
        # setelah verifikasi, publik boleh melihat
        self.admin.patch(f"/api/admin/laporan/{body['id']}/status", json={"status": "terverifikasi"}, headers=CSRF)
        self.assertEqual(self.publik.get(url).status_code, 200)
        # ditolak -> tertutup lagi
        self.admin.patch(f"/api/admin/laporan/{body['id']}/status", json={"status": "ditolak"}, headers=CSRF)
        self.assertEqual(self.publik.get(url).status_code, 404)

    def test_laporan_ditolak_tidak_tampil_publik(self):
        lid = self.kirim_laporan().get_json()["id"]
        self.admin.patch(f"/api/admin/laporan/{lid}/status", json={"status": "ditolak"}, headers=CSRF)
        self.assertEqual(self.publik.get("/api/laporan").get_json()["laporan"], [])

    def test_hapus_laporan_menghapus_berkas_foto(self):
        r = self.publik.post("/api/laporan", content_type="multipart/form-data", data={
            "jenis": "Titik Api Baru", "lokasi": "Desa X", "deskripsi": "Deskripsi cukup panjang.",
            "foto": (io.BytesIO(PNG), "a.png")})
        lid = r.get_json()["id"]
        self.assertEqual(len(os.listdir(self.app.config["UPLOAD_DIR"])), 1)
        self.assertEqual(self.admin.delete(f"/api/admin/laporan/{lid}", headers=CSRF).status_code, 200)
        self.assertEqual(os.listdir(self.app.config["UPLOAD_DIR"]), [])

    def test_sql_injection_tidak_berpengaruh(self):
        self.kirim_laporan(lokasi="x'); DROP TABLE laporan;--")
        self.assertEqual(self.publik.get("/api/laporan").status_code, 200)
        self.assertEqual(len(self.publik.get("/api/laporan").get_json()["laporan"]), 1)

    # ------------------------------------------------------------ langganan
    def test_langganan_dinormalisasi_dan_tidak_bocor(self):
        for nomor in ("0812-3456-7890", "+62 812 3456 7890", "081234567890"):
            r = self.publik.post("/api/langganan", json={"kontak": nomor, "peran": "Warga Lokal", "zona": "riau"})
            self.assertEqual(r.status_code, 201)
        daftar = self.admin.get("/api/admin/langganan").get_json()["langganan"]
        self.assertEqual([d["kontak"] for d in daftar], ["+6281234567890"])  # tidak dobel
        bad = self.publik.post("/api/langganan", json={"kontak": "123", "peran": "Warga Lokal", "zona": "riau"})
        self.assertEqual(bad.status_code, 400)

    # --------------------------------------------------------- data keuangan
    def test_admin_ubah_ringkasan_dan_sektor(self):
        r = self.admin.put("/api/admin/ringkasan", headers=CSRF, json={
            "pendapatan_triliun": 3000.5, "belanja_triliun": 3100, "silpa_triliun": 10})
        self.assertEqual(r.get_json()["ringkasan"]["pendapatan_triliun"], "Rp 3.000,50 T")

        baru = {"nama": "5. Pos Baru", "kelompok": "restorasi", "pagu_miliar": 200,
                "realisasi_miliar": 50, "deskripsi": "Uji coba", "warna": "#123456"}
        r = self.admin.post("/api/admin/sektor", json=baru, headers=CSRF)
        self.assertEqual(r.status_code, 201)
        sektor = r.get_json()["sektor_penyerapan"][-1]
        self.assertEqual((sektor["persentase"], sektor["realisasi_triliun"]), (25.0, 0.05))
        self.assertEqual(sektor["keterangan"], "Pagu: Rp 200 M • Uji coba")

        r = self.admin.put(f"/api/admin/sektor/{sektor['id']}", json={**baru, "realisasi_miliar": 100}, headers=CSRF)
        self.assertEqual(r.get_json()["sektor_penyerapan"][-1]["persentase"], 50.0)
        r = self.admin.delete(f"/api/admin/sektor/{sektor['id']}", headers=CSRF)
        self.assertEqual(len(r.get_json()["sektor_penyerapan"]), 4)

    def test_validasi_sektor(self):
        baru = {"nama": "Pos", "kelompok": "restorasi", "pagu_miliar": 100,
                "realisasi_miliar": 50, "deskripsi": "Uji", "warna": "#123456"}
        for rusak in ({"realisasi_miliar": 500}, {"warna": "red;background:url(x)"},
                      {"kelompok": "lain"}, {"pagu_miliar": "abc"}, {"pagu_miliar": float("nan")}):
            r = self.admin.post("/api/admin/sektor", data=json.dumps({**baru, **rusak}),
                                content_type="application/json", headers=CSRF)
            self.assertEqual(r.status_code, 400, rusak)

    def test_admin_kelola_pos_kas(self):
        r = self.admin.post("/api/admin/pos-kas", headers=CSRF,
                            json={"jenis": "masuk", "nama": "Pos Baru", "sumber": "Uji", "nilai_triliun": 1.5})
        self.assertEqual(r.get_json()["pos_kas"]["masuk"][-1]["nilai"], "Rp 1,50 T")
        pid = r.get_json()["pos_kas"]["masuk"][-1]["id"]
        r = self.admin.delete(f"/api/admin/pos-kas/{pid}", headers=CSRF)
        self.assertEqual(len(r.get_json()["pos_kas"]["masuk"]), 3)

    def test_data_awal_tidak_hidup_lagi_setelah_dihapus(self):
        self.admin.delete("/api/admin/sektor/bansos", headers=CSRF)
        app2 = create_app(self.dir)  # simulasi restart server
        self.assertEqual(len(app2.test_client().get("/api/anggaran").get_json()["sektor_penyerapan"]), 3)

    def test_error_api_berbentuk_json(self):
        r = self.publik.get("/api/tidak-ada")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())

    def test_halaman_admin_dan_data_tidak_terbuka_lewat_static(self):
        self.assertEqual(self.publik.get("/firewatch.db").status_code, 404)
        self.assertEqual(self.publik.get("/data/secret_key").status_code, 404)
        self.assertEqual(self.publik.get("/app.py").status_code, 404)


if __name__ == "__main__":
    unittest.main()
