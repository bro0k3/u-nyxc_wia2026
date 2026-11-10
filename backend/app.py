"""Backend FireWatch.

Menjalankan:  python app.py   ->  http://127.0.0.1:8000  (admin: /admin)

Aturan utama:
  * Siapa pun boleh MELIHAT data dan MENGIRIM laporan / mendaftar notifikasi.
  * Semua yang mengubah status laporan (verifikasi), mengubah data keuangan,
    atau melihat daftar kontak pendaftar HANYA bisa dilakukan admin yang sudah login.
    Pengecekannya ada di server (dekorator @admin_required), bukan di tombol HTML.
"""
import math
import os
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import database

BASE_DIR = Path(__file__).resolve().parent

# Papan laporan publik menampilkan laporan "menunggu" (dengan label menunggu).
# Ubah ke False kalau hanya laporan yang sudah diverifikasi yang boleh tampil.
TAMPILKAN_MENUNGGU_KE_PUBLIK = True

JENIS_LAPORAN = ("Titik Api Baru", "Dugaan Korupsi Anggaran", "Kebutuhan Logistik")
PERAN = ("Relawan Pemadam", "Warga Lokal", "Pemerhati Lingkungan")
ZONA_EWS = ("riau", "kalteng", "sumsel", "kalbar")
STATUS_LAPORAN = ("menunggu", "terverifikasi", "ditolak")
KELOMPOK = ("pemadaman", "restorasi")


class ValidasiError(Exception):
    """Input dari pengguna tidak valid -> dijawab 400 dengan pesan yang jelas."""


class Dilarang(Exception):
    """Permintaan ditolak (mis. header anti-CSRF hilang) -> dijawab 403."""


class PembatasLaju:
    """Pembatas sederhana di memori: maksimal N kejadian per jendela waktu."""

    def __init__(self):
        self.catatan = defaultdict(deque)

    def penuh(self, kunci, batas, detik):
        antrean = self.catatan[kunci]
        sekarang = time.monotonic()
        while antrean and sekarang - antrean[0] > detik:
            antrean.popleft()
        return len(antrean) >= batas

    def catat(self, kunci):
        self.catatan[kunci].append(time.monotonic())

    def hapus(self, kunci):
        self.catatan.pop(kunci, None)


# ------------------------------------------------------------------ validasi
def teks_bersih(nilai, nama, minimal, maksimal, banyak_baris=False):
    if not isinstance(nilai, str):
        raise ValidasiError(f"{nama} wajib diisi.")
    nilai = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", nilai)  # buang karakter kontrol
    if banyak_baris:
        nilai = nilai.strip()
    else:
        nilai = " ".join(nilai.split())
    if len(nilai) < minimal:
        raise ValidasiError(f"{nama} minimal {minimal} karakter.")
    if len(nilai) > maksimal:
        raise ValidasiError(f"{nama} maksimal {maksimal} karakter.")
    return nilai


def angka(nilai, nama, minimal=None, maksimal=None):
    if isinstance(nilai, bool) or nilai is None or nilai == "":
        raise ValidasiError(f"{nama} harus berupa angka.")
    try:
        hasil = float(nilai)
    except (TypeError, ValueError):
        raise ValidasiError(f"{nama} harus berupa angka.") from None
    if not math.isfinite(hasil):
        raise ValidasiError(f"{nama} harus berupa angka.")
    if minimal is not None and hasil < minimal:
        raise ValidasiError(f"{nama} tidak boleh kurang dari {minimal}.")
    if maksimal is not None and hasil > maksimal:
        raise ValidasiError(f"{nama} tidak boleh lebih dari {maksimal}.")
    return hasil


def baca_koordinat(lat, lng):
    lat = (lat or "").strip() if isinstance(lat, str) else lat
    lng = (lng or "").strip() if isinstance(lng, str) else lng
    if lat in (None, "") and lng in (None, ""):
        return None, None
    return angka(lat, "Latitude", -90, 90), angka(lng, "Longitude", -180, 180)


def normalisasi_hp(teks):
    """'0812-3456-7890' / '+62 812...' -> '+62812...'"""
    if not isinstance(teks, str):
        raise ValidasiError("Nomor HP wajib diisi.")
    digit = re.sub(r"[\s\-().]", "", teks)
    if digit.startswith("+"):
        digit = digit[1:]
    if digit.startswith("0"):
        digit = "62" + digit[1:]
    if not re.fullmatch(r"628\d{8,11}", digit):
        raise ValidasiError("Gunakan nomor HP Indonesia, contoh: 081234567890.")
    return "+" + digit


def deteksi_ekstensi(kepala):
    """Kenali jenis gambar dari isi berkasnya (bukan dari nama / ekstensi kiriman)."""
    if kepala.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if kepala.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if kepala[:4] == b"RIFF" and kepala[8:12] == b"WEBP":
        return ".webp"
    return None


def create_app(data_dir=None):
    data_dir = Path(data_dir or os.environ.get("FIREWATCH_DATA_DIR") or BASE_DIR / "data")
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)

    app = Flask(__name__, static_folder=str(BASE_DIR / "public"), static_url_path="")
    app.config.update(
        SECRET_KEY=muat_secret_key(data_dir),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # batas ukuran kiriman (foto) 5 MB
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("FIREWATCH_HTTPS") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        DB_PATH=str(data_dir / "firewatch.db"),
        UPLOAD_DIR=str(data_dir / "uploads"),
    )
    database.init_db(app.config["DB_PATH"], BASE_DIR / "seed" / "anggaran.json")

    limiter = PembatasLaju()
    app.limiter = limiter
    # Hash palsu: supaya waktu login sama lamanya baik username ada maupun tidak.
    hash_palsu = generate_password_hash(secrets.token_hex(8))

    # ------------------------------------------------------------ database
    def get_db():
        if "db" not in g:
            g.db = database.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def tutup_db(_error):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    # ------------------------------------------------------------ autentikasi
    def admin_saat_ini():
        admin_id = session.get("admin_id")
        if admin_id is None:
            return None
        row = get_db().execute(
            "SELECT id, username FROM admin WHERE id = ?", (admin_id,)
        ).fetchone()
        if row is None:  # akun admin sudah dihapus -> sesi tidak berlaku lagi
            session.clear()
        return row

    def periksa_header_csrf():
        # Header kustom tidak bisa dikirim situs lain tanpa izin browser,
        # jadi permintaan palsu dari situs jahat otomatis ditolak.
        if request.headers.get("X-Requested-With") != "FireWatch":
            raise Dilarang("Permintaan tidak sah (header X-Requested-With hilang).")

    def admin_required(fn):
        @wraps(fn)
        def pembungkus(*args, **kwargs):
            admin = admin_saat_ini()
            if admin is None:
                return jsonify(error="Harus login sebagai admin."), 401
            if request.method not in ("GET", "HEAD"):
                periksa_header_csrf()
            g.admin = admin
            return fn(*args, **kwargs)
        return pembungkus

    def body_json():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValidasiError("Isi permintaan harus berupa JSON.")
        return data

    # ------------------------------------------------------------ penanganan error
    @app.errorhandler(ValidasiError)
    def galat_validasi(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(Dilarang)
    def galat_dilarang(e):
        return jsonify(error=str(e)), 403

    @app.errorhandler(HTTPException)
    def galat_http(e):
        if request.path.startswith("/api/"):
            pesan = "Ukuran kiriman melebihi 5 MB." if e.code == 413 else e.description
            return jsonify(error=pesan), e.code
        return e

    @app.after_request
    def header_keamanan(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------ halaman
    @app.get("/")
    def beranda():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/admin")
    def halaman_admin():
        return send_from_directory(app.static_folder, "admin.html")

    # =================================================================
    #  API PUBLIK
    # =================================================================
    @app.get("/api/anggaran")
    def api_anggaran():
        return jsonify(database.ambil_anggaran(get_db()))

    def laporan_ke_dict(row, admin=False):
        terverifikasi = row["status"] == "terverifikasi"
        data = {
            "id": row["id"],
            "jenis": row["jenis"],
            "lokasi": row["lokasi"],
            "lat": row["lat"],
            "lng": row["lng"],
            "deskripsi": row["deskripsi"],
            "status": row["status"],
            "dibuat_pada": row["dibuat_pada"],
            "diverifikasi_pada": row["ditinjau_pada"] if terverifikasi else None,
            "ada_foto": bool(row["foto"]),
            # Foto laporan yang belum diverifikasi hanya boleh dilihat admin.
            "foto_url": f"/uploads/{row['foto']}" if row["foto"] and (terverifikasi or admin) else None,
        }
        if admin:
            data["ditinjau_oleh"] = row["ditinjau_oleh"]
            data["ditinjau_pada"] = row["ditinjau_pada"]
        return data

    def hitung_status():
        hasil = {s: 0 for s in STATUS_LAPORAN}
        for row in get_db().execute("SELECT status, COUNT(*) AS n FROM laporan GROUP BY status"):
            hasil[row["status"]] = row["n"]
        return hasil

    @app.get("/api/laporan")
    def daftar_laporan():
        batas = min(max(request.args.get("limit", 50, type=int), 1), 100)
        if TAMPILKAN_MENUNGGU_KE_PUBLIK:
            rows = get_db().execute(
                "SELECT * FROM laporan WHERE status IN ('terverifikasi', 'menunggu')"
                " ORDER BY dibuat_pada DESC, id DESC LIMIT ?", (batas,)).fetchall()
        else:
            rows = get_db().execute(
                "SELECT * FROM laporan WHERE status = 'terverifikasi'"
                " ORDER BY dibuat_pada DESC, id DESC LIMIT ?", (batas,)).fetchall()
        jumlah = hitung_status()
        return jsonify(
            laporan=[laporan_ke_dict(r) for r in rows],
            jumlah={"terverifikasi": jumlah["terverifikasi"], "menunggu": jumlah["menunggu"]},
        )

    def simpan_foto(berkas):
        if berkas is None or not berkas.filename:
            return None
        ekstensi = deteksi_ekstensi(berkas.stream.read(12))
        berkas.stream.seek(0)
        if ekstensi is None:
            raise ValidasiError("Foto harus berformat JPG, PNG, atau WebP.")
        nama = uuid.uuid4().hex + ekstensi  # nama acak: nama asli dari pengguna tidak dipakai
        berkas.save(os.path.join(app.config["UPLOAD_DIR"], nama))
        return nama

    @app.post("/api/laporan")
    def kirim_laporan():
        kunci = f"laporan:{request.remote_addr}"
        if limiter.penuh(kunci, 5, 600):
            return jsonify(error="Terlalu banyak laporan dari perangkat ini. Coba lagi beberapa menit lagi."), 429
        limiter.catat(kunci)

        jenis = request.form.get("jenis", "")
        if jenis not in JENIS_LAPORAN:
            raise ValidasiError("Jenis laporan tidak dikenal.")
        lokasi = teks_bersih(request.form.get("lokasi"), "Lokasi", 3, 200)
        deskripsi = teks_bersih(request.form.get("deskripsi"), "Deskripsi", 10, 2000, banyak_baris=True)
        lat, lng = baca_koordinat(request.form.get("lat"), request.form.get("lng"))
        foto = simpan_foto(request.files.get("foto"))  # terakhir, setelah teks lolos validasi

        try:
            conn = get_db()
            cur = conn.execute(
                "INSERT INTO laporan (jenis, lokasi, lat, lng, deskripsi, foto, dibuat_pada)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (jenis, lokasi, lat, lng, deskripsi, foto, database.sekarang()),
            )
            conn.commit()
        except Exception:
            if foto:
                hapus_berkas_foto(foto)
            raise
        row = conn.execute("SELECT * FROM laporan WHERE id = ?", (cur.lastrowid,)).fetchone()
        return jsonify(laporan_ke_dict(row)), 201

    @app.post("/api/langganan")
    def daftar_langganan():
        kunci = f"langganan:{request.remote_addr}"
        if limiter.penuh(kunci, 10, 600):
            return jsonify(error="Terlalu banyak percobaan. Coba lagi beberapa menit lagi."), 429
        limiter.catat(kunci)

        data = body_json()
        kontak = normalisasi_hp(data.get("kontak"))
        if data.get("peran") not in PERAN:
            raise ValidasiError("Peran tidak dikenal.")
        if data.get("zona") not in ZONA_EWS:
            raise ValidasiError("Zona tidak dikenal.")
        conn = get_db()
        # Nomor yang sudah terdaftar dijawab sama seperti yang baru, supaya orang lain
        # tidak bisa mengecek siapa saja yang sudah mendaftar.
        conn.execute(
            "INSERT OR IGNORE INTO langganan (kontak, peran, zona, dibuat_pada) VALUES (?, ?, ?, ?)",
            (kontak, data["peran"], data["zona"], database.sekarang()),
        )
        conn.commit()
        return jsonify(pesan="Pendaftaran notifikasi diterima."), 201

    @app.get("/uploads/<nama>")
    def lihat_foto(nama):
        if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|webp)", nama):
            abort(404)
        row = get_db().execute("SELECT status FROM laporan WHERE foto = ?", (nama,)).fetchone()
        if row is None:
            abort(404)
        if row["status"] != "terverifikasi" and admin_saat_ini() is None:
            abort(404)  # foto laporan yang belum diverifikasi tidak dibuka untuk umum
        resp = send_from_directory(app.config["UPLOAD_DIR"], nama)
        resp.headers["Cache-Control"] = "private, max-age=300"
        return resp

    def hapus_berkas_foto(nama):
        try:
            os.remove(os.path.join(app.config["UPLOAD_DIR"], nama))
        except OSError:
            pass

    # =================================================================
    #  API ADMIN
    # =================================================================
    @app.get("/api/admin/me")
    def admin_me():
        admin = admin_saat_ini()
        return jsonify(admin=admin is not None, username=admin["username"] if admin else None)

    @app.post("/api/admin/login")
    def admin_login():
        periksa_header_csrf()
        kunci = f"login:{request.remote_addr}"
        if limiter.penuh(kunci, 5, 900):
            return jsonify(error="Terlalu banyak percobaan login. Coba lagi 15 menit lagi."), 429

        data = body_json()
        username = str(data.get("username", "")).strip()[:50]
        password = str(data.get("password", ""))[:200]
        row = get_db().execute("SELECT * FROM admin WHERE username = ?", (username,)).fetchone()
        cocok = check_password_hash(row["password_hash"] if row else hash_palsu, password)
        if row is None or not cocok:
            limiter.catat(kunci)
            return jsonify(error="Username atau kata sandi salah."), 401

        limiter.hapus(kunci)
        session.clear()  # sesi baru setiap login
        session["admin_id"] = row["id"]
        session.permanent = True
        return jsonify(admin=True, username=row["username"])

    @app.post("/api/admin/logout")
    def admin_logout():
        periksa_header_csrf()
        session.clear()
        return jsonify(admin=False)

    # ---- laporan warga: verifikasi / tolak / hapus -------------------
    @app.get("/api/admin/laporan")
    @admin_required
    def admin_daftar_laporan():
        status = request.args.get("status", "semua")
        if status != "semua" and status not in STATUS_LAPORAN:
            raise ValidasiError("Filter status tidak dikenal.")
        sql = "SELECT * FROM laporan"
        params = []
        if status != "semua":
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY dibuat_pada DESC, id DESC LIMIT 200"
        rows = get_db().execute(sql, params).fetchall()
        return jsonify(
            laporan=[laporan_ke_dict(r, admin=True) for r in rows],
            jumlah=hitung_status(),
        )

    @app.patch("/api/admin/laporan/<int:laporan_id>/status")
    @admin_required
    def admin_ubah_status(laporan_id):
        status = body_json().get("status")
        if status not in STATUS_LAPORAN:
            raise ValidasiError("Status harus 'menunggu', 'terverifikasi', atau 'ditolak'.")
        conn = get_db()
        if status == "menunggu":
            oleh, pada = None, None
        else:
            oleh, pada = g.admin["username"], database.sekarang()
        cur = conn.execute(
            "UPDATE laporan SET status = ?, ditinjau_oleh = ?, ditinjau_pada = ? WHERE id = ?",
            (status, oleh, pada, laporan_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify(error="Laporan tidak ditemukan."), 404
        row = conn.execute("SELECT * FROM laporan WHERE id = ?", (laporan_id,)).fetchone()
        return jsonify(laporan_ke_dict(row, admin=True))

    @app.delete("/api/admin/laporan/<int:laporan_id>")
    @admin_required
    def admin_hapus_laporan(laporan_id):
        conn = get_db()
        row = conn.execute("SELECT foto FROM laporan WHERE id = ?", (laporan_id,)).fetchone()
        if row is None:
            return jsonify(error="Laporan tidak ditemukan."), 404
        conn.execute("DELETE FROM laporan WHERE id = ?", (laporan_id,))
        conn.commit()
        if row["foto"]:
            hapus_berkas_foto(row["foto"])
        return jsonify(terhapus=laporan_id)

    # ---- pendaftar notifikasi (berisi nomor HP -> hanya admin) ---------
    @app.get("/api/admin/langganan")
    @admin_required
    def admin_daftar_langganan():
        rows = get_db().execute("SELECT * FROM langganan ORDER BY id DESC LIMIT 500").fetchall()
        return jsonify(langganan=[dict(r) for r in rows])

    @app.delete("/api/admin/langganan/<int:langganan_id>")
    @admin_required
    def admin_hapus_langganan(langganan_id):
        conn = get_db()
        cur = conn.execute("DELETE FROM langganan WHERE id = ?", (langganan_id,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify(error="Data tidak ditemukan."), 404
        return jsonify(terhapus=langganan_id)

    # ---- data keuangan -------------------------------------------------
    @app.put("/api/admin/ringkasan")
    @admin_required
    def admin_ubah_ringkasan():
        data = body_json()
        nilai = [angka(data.get(k), nama, 0, 1_000_000) for k, nama in (
            ("pendapatan_triliun", "Pendapatan"), ("belanja_triliun", "Belanja"), ("silpa_triliun", "SILPA"))]
        conn = get_db()
        conn.execute(
            "INSERT INTO ringkasan (id, pendapatan_triliun, belanja_triliun, silpa_triliun)"
            " VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET"
            " pendapatan_triliun = excluded.pendapatan_triliun,"
            " belanja_triliun = excluded.belanja_triliun,"
            " silpa_triliun = excluded.silpa_triliun", nilai)
        conn.commit()
        return jsonify(database.ambil_anggaran(conn))

    def baca_sektor(data):
        pagu = angka(data.get("pagu_miliar"), "Pagu", 0.01, 10_000_000)
        realisasi = angka(data.get("realisasi_miliar"), "Realisasi", 0, 10_000_000)
        if realisasi > pagu:
            raise ValidasiError("Realisasi tidak boleh melebihi pagu.")
        warna = data.get("warna")
        if not isinstance(warna, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", warna):
            raise ValidasiError("Warna harus berformat hex, contoh #0B3D2E.")
        if data.get("kelompok") not in KELOMPOK:
            raise ValidasiError("Kelompok harus 'pemadaman' atau 'restorasi'.")
        return {
            "nama": teks_bersih(data.get("nama"), "Nama pos", 3, 120),
            "kelompok": data["kelompok"],
            "pagu_miliar": pagu,
            "realisasi_miliar": realisasi,
            "deskripsi": teks_bersih(data.get("deskripsi"), "Keterangan", 3, 150),
            "warna": warna,
        }

    @app.post("/api/admin/sektor")
    @admin_required
    def admin_tambah_sektor():
        s = baca_sektor(body_json())
        conn = get_db()
        urutan = conn.execute("SELECT COALESCE(MAX(urutan), 0) + 1 FROM sektor").fetchone()[0]
        conn.execute(
            "INSERT INTO sektor (id, urutan, nama, kelompok, pagu_miliar, realisasi_miliar, deskripsi, warna)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("sektor-" + secrets.token_hex(3), urutan, s["nama"], s["kelompok"],
             s["pagu_miliar"], s["realisasi_miliar"], s["deskripsi"], s["warna"]))
        conn.commit()
        return jsonify(database.ambil_anggaran(conn)), 201

    @app.put("/api/admin/sektor/<sektor_id>")
    @admin_required
    def admin_ubah_sektor(sektor_id):
        s = baca_sektor(body_json())
        conn = get_db()
        cur = conn.execute(
            "UPDATE sektor SET nama = ?, kelompok = ?, pagu_miliar = ?, realisasi_miliar = ?,"
            " deskripsi = ?, warna = ? WHERE id = ?",
            (s["nama"], s["kelompok"], s["pagu_miliar"], s["realisasi_miliar"],
             s["deskripsi"], s["warna"], sektor_id))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify(error="Pos tidak ditemukan."), 404
        return jsonify(database.ambil_anggaran(conn))

    @app.delete("/api/admin/sektor/<sektor_id>")
    @admin_required
    def admin_hapus_sektor(sektor_id):
        conn = get_db()
        cur = conn.execute("DELETE FROM sektor WHERE id = ?", (sektor_id,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify(error="Pos tidak ditemukan."), 404
        return jsonify(database.ambil_anggaran(conn))

    def baca_pos_kas(data):
        if data.get("jenis") not in ("masuk", "keluar"):
            raise ValidasiError("Jenis harus 'masuk' atau 'keluar'.")
        return (
            data["jenis"],
            teks_bersih(data.get("nama"), "Nama pos", 3, 120),
            teks_bersih(data.get("sumber"), "Sumber", 3, 120),
            angka(data.get("nilai_triliun"), "Nilai", 0, 1_000_000),
        )

    @app.post("/api/admin/pos-kas")
    @admin_required
    def admin_tambah_pos_kas():
        conn = get_db()
        conn.execute("INSERT INTO pos_kas (jenis, nama, sumber, nilai_triliun) VALUES (?, ?, ?, ?)",
                     baca_pos_kas(body_json()))
        conn.commit()
        return jsonify(database.ambil_anggaran(conn)), 201

    @app.put("/api/admin/pos-kas/<int:pos_id>")
    @admin_required
    def admin_ubah_pos_kas(pos_id):
        conn = get_db()
        cur = conn.execute(
            "UPDATE pos_kas SET jenis = ?, nama = ?, sumber = ?, nilai_triliun = ? WHERE id = ?",
            (*baca_pos_kas(body_json()), pos_id))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify(error="Pos tidak ditemukan."), 404
        return jsonify(database.ambil_anggaran(conn))

    @app.delete("/api/admin/pos-kas/<int:pos_id>")
    @admin_required
    def admin_hapus_pos_kas(pos_id):
        conn = get_db()
        cur = conn.execute("DELETE FROM pos_kas WHERE id = ?", (pos_id,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify(error="Pos tidak ditemukan."), 404
        return jsonify(database.ambil_anggaran(conn))

    return app


def muat_secret_key(data_dir):
    """Kunci penanda tangan cookie sesi. Pakai variabel SECRET_KEY kalau ada,
    kalau tidak, buat sekali dan simpan di data/secret_key (jangan dibagikan)."""
    dari_env = os.environ.get("SECRET_KEY")
    if dari_env:
        return dari_env
    jalur = Path(data_dir) / "secret_key"
    if not jalur.exists():
        jalur.write_text(secrets.token_hex(32))
        try:
            os.chmod(jalur, 0o600)
        except OSError:
            pass
    return jalur.read_text().strip()


app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
