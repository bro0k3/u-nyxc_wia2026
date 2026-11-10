# FireWatch — Backend

Backend untuk situs FireWatch: menyimpan data keuangan, laporan warga (dengan foto),
dan pendaftar notifikasi, **dengan verifikasi laporan yang hanya bisa dilakukan admin**.

Teknologi: Python + Flask + SQLite. Tidak perlu memasang database terpisah —
semua data tersimpan di satu berkas, `data/firewatch.db`.

## Isi folder

```
app.py            server + semua endpoint API
database.py       tabel database & data awal (dari seed/anggaran.json)
manage.py         perintah terminal: buat akun admin, dll.
test_app.py       tes otomatis (python -m unittest -v)
seed/             anggaran.json -> hanya dipakai sekali saat database pertama dibuat
public/           index.html (situs), admin.html (panel admin)
data/             dibuat otomatis: firewatch.db, uploads/ (foto), secret_key
```

## Cara menjalankan

1. Pasang Python 3.10+ lalu, di folder ini:

   ```
   python -m venv .venv
   .venv\Scripts\activate          (Windows)   |   source .venv/bin/activate   (Mac/Linux)
   pip install -r requirements.txt
   ```

2. Salin folder `pic/` dan `vid/` ke dalam folder `public/`
   (supaya logo, foto, dan video tetap muncul).

3. Buat akun admin pertama (kata sandi minimal 10 karakter):

   ```
   python manage.py buat-admin namaadmin
   ```

4. Jalankan:

   ```
   python app.py
   ```

   - Situs publik: http://127.0.0.1:8000
   - Panel admin: http://127.0.0.1:8000/admin

   Opsional, untuk demo: `python manage.py contoh-laporan` mengisi 2 laporan contoh.

## Siapa boleh apa

| Aksi | Publik | Admin |
|---|---|---|
| Melihat anggaran, Buku Kas, papan laporan | ✅ | ✅ |
| Mengirim laporan, mendaftar notifikasi | ✅ | ✅ |
| **Verifikasi / tolak / hapus laporan** | ❌ | ✅ |
| Mengubah data keuangan | ❌ | ✅ |
| Melihat nomor HP pendaftar notifikasi | ❌ | ✅ |
| Melihat foto laporan yang belum diverifikasi | ❌ | ✅ |

Aturan ini dicek **di server** (dekorator `@admin_required` di `app.py`), bukan hanya
dengan menyembunyikan tombol. Orang yang memanggil API langsung tetap mendapat `401`.

- Akun admin hanya bisa dibuat lewat terminal (`manage.py`); tidak ada halaman daftar admin.
- Kata sandi disimpan sebagai hash (scrypt), tidak pernah sebagai teks biasa.
- Login memakai cookie sesi bertanda tangan (HttpOnly, SameSite=Lax, berlaku 8 jam).
  Maksimal 5 kali salah login per 15 menit per alamat IP.
- Setiap perubahan admin wajib membawa header `X-Requested-With: FireWatch`
  (perlindungan CSRF; sudah dikirim otomatis oleh `admin.html`).
- Siapa yang memverifikasi dan kapan tersimpan di database (`ditinjau_oleh`, `ditinjau_pada`).

## Daftar API

Publik:

| Method | URL | Fungsi |
|---|---|---|
| GET | `/api/anggaran` | ringkasan, pos penyerapan, Buku Kas (menggantikan `anggaran.json`) |
| GET | `/api/laporan` | papan laporan (terverifikasi + menunggu) |
| POST | `/api/laporan` | kirim laporan (form-data: jenis, lokasi, deskripsi, lat, lng, foto) |
| POST | `/api/langganan` | daftar notifikasi (JSON: kontak, peran, zona) |

Admin (wajib login):

| Method | URL | Fungsi |
|---|---|---|
| POST | `/api/admin/login`, `/api/admin/logout` | masuk / keluar |
| GET | `/api/admin/me` | cek status login |
| GET | `/api/admin/laporan?status=menunggu` | daftar laporan (semua status) |
| PATCH | `/api/admin/laporan/<id>/status` | `{"status": "terverifikasi" / "ditolak" / "menunggu"}` |
| DELETE | `/api/admin/laporan/<id>` | hapus laporan + fotonya |
| PUT | `/api/admin/ringkasan` | ubah pendapatan, belanja, SILPA |
| POST / PUT / DELETE | `/api/admin/sektor[/<id>]` | kelola pos penyerapan anggaran |
| POST / PUT / DELETE | `/api/admin/pos-kas[/<id>]` | kelola rincian uang masuk / keluar |
| GET / DELETE | `/api/admin/langganan[/<id>]` | lihat / hapus pendaftar notifikasi |

## Perubahan pada index.html

- Data anggaran, Buku Kas, laporan, dan pendaftaran notifikasi kini dibaca/dikirim ke server.
- Tombol "Verifikasi (Simulasi Admin)" dihapus dari halaman publik; verifikasi dilakukan di `/admin`.
- Semua teks dari server disisipkan dengan `esc()` agar tidak bisa menyuntik HTML/skrip (XSS).
- Tombol filter grafik memakai kolom `kelompok` dari data, bukan angka yang ditulis manual
  (jadi grafik ikut benar saat admin mengubah angka atau menambah pos).
- "Ambil GPS" tidak lagi mengarang lokasi saat GPS gagal; arah koordinat (U/S, B/T) juga diperbaiki.
  GPS browser hanya berfungsi di `localhost` atau HTTPS.

## Pengaturan

| Pengaturan | Cara | Bawaan |
|---|---|---|
| Port / host | env `PORT`, `HOST` | 8000 / 127.0.0.1 |
| Lokasi data | env `FIREWATCH_DATA_DIR` | `./data` |
| Kunci sesi | env `SECRET_KEY` | dibuat otomatis di `data/secret_key` |
| Cookie hanya lewat HTTPS | env `FIREWATCH_HTTPS=1` | mati |
| Tampilkan laporan "menunggu" ke publik | `TAMPILKAN_MENUNGGU_KE_PUBLIK` di `app.py` | `True` |

## Sebelum dipasang di internet (produksi)

- Pakai HTTPS dan set `FIREWATCH_HTTPS=1`; jalankan lewat server produksi
  (mis. `pip install gunicorn` lalu `gunicorn -w 1 app:app`), bukan `python app.py`.
- Pembatas laju disimpan di memori per proses (pakai 1 worker, atau ganti dengan Redis).
  Kalau di belakang proxy (Nginx, dll.), alamat IP perlu diteruskan dengan `werkzeug.middleware.proxy_fix.ProxyFix`.
- Cadangkan folder `data/` secara berkala (berisi database dan foto).
- Simpan `data/secret_key` rahasia; jangan masuk Git (sudah ada di `.gitignore`).
