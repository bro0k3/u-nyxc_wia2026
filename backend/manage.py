"""Perintah pengelolaan FireWatch (dijalankan dari terminal, bukan dari web).

    python manage.py buat-admin <username>       buat akun admin baru
    python manage.py ganti-password <username>   ganti kata sandi admin
    python manage.py daftar-admin                tampilkan semua admin
    python manage.py contoh-laporan              isi 2 laporan contoh (untuk demo)

Akun admin sengaja HANYA bisa dibuat lewat terminal: tidak ada halaman daftar
admin di web, jadi orang luar tidak mungkin mendaftarkan dirinya sebagai admin.
"""
import getpass
import re
import sys

from werkzeug.security import generate_password_hash

import database
from app import app

MIN_PANJANG_PASSWORD = 10


def minta_password(username):
    sandi = getpass.getpass("Kata sandi: ")
    if len(sandi) < MIN_PANJANG_PASSWORD:
        sys.exit(f"Kata sandi minimal {MIN_PANJANG_PASSWORD} karakter.")
    if sandi.lower() == username.lower():
        sys.exit("Kata sandi tidak boleh sama dengan username.")
    if getpass.getpass("Ulangi kata sandi: ") != sandi:
        sys.exit("Kedua kata sandi tidak sama.")
    return generate_password_hash(sandi)


def koneksi():
    return database.connect(app.config["DB_PATH"])


def buat_admin(username):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username):
        sys.exit("Username 3-50 karakter: huruf, angka, titik, garis bawah, atau strip.")
    conn = koneksi()
    if conn.execute("SELECT 1 FROM admin WHERE username = ?", (username,)).fetchone():
        sys.exit(f"Admin '{username}' sudah ada. Pakai: python manage.py ganti-password {username}")
    conn.execute(
        "INSERT INTO admin (username, password_hash, dibuat_pada) VALUES (?, ?, ?)",
        (username, minta_password(username), database.sekarang()))
    conn.commit()
    print(f"Admin '{username}' dibuat. Login di http://127.0.0.1:8000/admin")


def ganti_password(username):
    conn = koneksi()
    if not conn.execute("SELECT 1 FROM admin WHERE username = ?", (username,)).fetchone():
        sys.exit(f"Admin '{username}' tidak ditemukan.")
    conn.execute("UPDATE admin SET password_hash = ? WHERE username = ?",
                 (minta_password(username), username))
    conn.commit()
    print("Kata sandi diganti.")


def daftar_admin():
    for row in koneksi().execute("SELECT username, dibuat_pada FROM admin ORDER BY id"):
        print(f"{row['username']}  (dibuat {row['dibuat_pada']})")


def contoh_laporan():
    conn = koneksi()
    sekarang = database.sekarang()
    conn.execute(
        "INSERT INTO laporan (jenis, lokasi, lat, lng, deskripsi, status, dibuat_pada, ditinjau_oleh, ditinjau_pada)"
        " VALUES (?, ?, ?, ?, ?, 'terverifikasi', ?, 'contoh', ?)",
        ("Titik Api Baru", "Desa Jabiren, Pulang Pisau", -2.7210, 114.2811,
         "Terlihat asap tebal membubung di perbatasan area gambut Desa Jabiren. Butuh pompa tambahan.",
         sekarang, sekarang))
    conn.execute(
        "INSERT INTO laporan (jenis, lokasi, lat, lng, deskripsi, status, dibuat_pada)"
        " VALUES (?, ?, ?, ?, ?, 'menunggu', ?)",
        ("Dugaan Korupsi Anggaran", "Posko Desa 2, Bengkalis", 1.4820, 102.1390,
         "Peralatan pompa air posko desa 2 belum menerima bantuan selang baru sesuai alokasi APBN.",
         sekarang))
    conn.commit()
    print("2 laporan contoh ditambahkan.")


PERINTAH = {
    "buat-admin": (buat_admin, 1),
    "ganti-password": (ganti_password, 1),
    "daftar-admin": (daftar_admin, 0),
    "contoh-laporan": (contoh_laporan, 0),
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in PERINTAH:
        sys.exit(__doc__)
    fungsi, jumlah_argumen = PERINTAH[sys.argv[1]]
    if len(sys.argv) - 2 != jumlah_argumen:
        sys.exit(__doc__)
    fungsi(*sys.argv[2:])
