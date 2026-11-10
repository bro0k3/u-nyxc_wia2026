"""Lapisan database FireWatch (SQLite).

Berisi: skema tabel, pengisian data awal dari seed/anggaran.json (sekali saja),
dan fungsi-fungsi pembantu untuk memformat angka ke gaya Indonesia.
"""
import json
import math
import re
import sqlite3
from datetime import datetime, timezone

SKEMA = """
CREATE TABLE IF NOT EXISTS admin (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    dibuat_pada   TEXT NOT NULL
);

-- Satu baris saja (id selalu 1): angka ringkasan Buku Kas.
CREATE TABLE IF NOT EXISTS ringkasan (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    pendapatan_triliun REAL NOT NULL,
    belanja_triliun    REAL NOT NULL,
    silpa_triliun      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sektor (
    id               TEXT PRIMARY KEY,
    urutan           INTEGER NOT NULL,
    nama             TEXT NOT NULL,
    kelompok         TEXT NOT NULL CHECK (kelompok IN ('pemadaman', 'restorasi')),
    pagu_miliar      REAL NOT NULL CHECK (pagu_miliar > 0),
    realisasi_miliar REAL NOT NULL CHECK (realisasi_miliar >= 0),
    deskripsi        TEXT NOT NULL,
    warna            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pos_kas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    jenis         TEXT NOT NULL CHECK (jenis IN ('masuk', 'keluar')),
    nama          TEXT NOT NULL,
    sumber        TEXT NOT NULL,
    nilai_triliun REAL NOT NULL CHECK (nilai_triliun >= 0)
);

CREATE TABLE IF NOT EXISTS laporan (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    jenis         TEXT NOT NULL,
    lokasi        TEXT NOT NULL,
    lat           REAL,
    lng           REAL,
    deskripsi     TEXT NOT NULL,
    foto          TEXT,
    status        TEXT NOT NULL DEFAULT 'menunggu'
                  CHECK (status IN ('menunggu', 'terverifikasi', 'ditolak')),
    dibuat_pada   TEXT NOT NULL,
    ditinjau_oleh TEXT,   -- username admin yang memverifikasi / menolak
    ditinjau_pada TEXT
);
CREATE INDEX IF NOT EXISTS idx_laporan_status ON laporan (status, dibuat_pada DESC);

CREATE TABLE IF NOT EXISTS langganan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kontak      TEXT NOT NULL,
    peran       TEXT NOT NULL,
    zona        TEXT NOT NULL,
    dibuat_pada TEXT NOT NULL,
    UNIQUE (kontak, zona)
);
"""

# Pos Buku Kas yang sebelumnya tertulis langsung di index.html.
POS_KAS_AWAL = [
    ("masuk", "Pendapatan Perpajakan", "LKPP 2025 Audited", 2218.17),
    ("masuk", "PNBP (Penerimaan Negara Bukan Pajak)", "LKPP 2025 Audited", 541.53),
    ("masuk", "Penerimaan Hibah", "LKPP 2025 Audited", 5.43),
    ("keluar", "Belanja Pemerintah Pusat", "LKPP 2025 Audited", 2586.42),
    ("keluar", "Transfer ke Daerah", "LKPP 2025 Audited", 849.04),
    ("keluar", "Pembiayaan Neto", "LKPP 2025 Audited", 742.73),
]

# Dipakai tombol filter grafik ("Pemadaman Darurat" / "Restorasi Gambut").
KELOMPOK_AWAL = {
    "pemadaman": "pemadaman",
    "logistik": "pemadaman",
    "restorasi": "restorasi",
    "bansos": "restorasi",
}


def sekarang():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ---------------------------------------------------------------- format angka
def format_angka(nilai, desimal=2):
    """2765.13 -> '2.765,13' (titik ribuan, koma desimal)."""
    teks = f"{nilai:,.{desimal}f}"
    return teks.replace(",", "_").replace(".", ",").replace("_", ".")


def rupiah_triliun(nilai):
    return f"Rp {format_angka(nilai, 2)} T"


def rupiah_miliar(nilai):
    desimal = 0 if float(nilai).is_integer() else 2
    return f"Rp {format_angka(nilai, desimal)} M"


def parse_triliun(teks):
    """'Rp 2.765,13 T' -> 2765.13"""
    bersih = re.sub(r"[^0-9,.]", "", teks)
    return float(bersih.replace(".", "").replace(",", "."))


def hitung_persen(realisasi, pagu):
    """Persentase penyerapan, dipotong (bukan dibulatkan) ke 1 desimal.

    Dipotong supaya hasilnya sama persis dengan anggaran.json awal
    (620 / 732 = 84,69% ditampilkan 84,6%) dan tidak melebih-lebihkan penyerapan.
    """
    return math.floor(realisasi / pagu * 1000 + 1e-9) / 10


# ------------------------------------------------------------- buat & isi awal
def init_db(path, seed_path):
    conn = connect(path)
    try:
        conn.executescript(SKEMA)
        # user_version = 0 berarti database baru -> isi data awal SEKALI saja.
        # (Kalau admin menghapus sebuah pos, ia tidak "hidup lagi" saat restart.)
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            _isi_data_awal(conn, seed_path)
            conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


def _isi_data_awal(conn, seed_path):
    try:
        with open(seed_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return

    r = data["ringkasan"]
    conn.execute(
        "INSERT INTO ringkasan (id, pendapatan_triliun, belanja_triliun, silpa_triliun)"
        " VALUES (1, ?, ?, ?)",
        (parse_triliun(r["pendapatan_triliun"]),
         parse_triliun(r["belanja_triliun"]),
         parse_triliun(r["silpa_triliun"])),
    )
    for urutan, s in enumerate(data["sektor_penyerapan"], start=1):
        # "Pagu: Rp 1.536 M • Realisasi Darurat Karhutla" -> simpan bagian setelah "•"
        deskripsi = s["keterangan"].split("•", 1)[-1].strip()
        conn.execute(
            "INSERT INTO sektor (id, urutan, nama, kelompok, pagu_miliar,"
            " realisasi_miliar, deskripsi, warna) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (s["id"], urutan, s["nama"], KELOMPOK_AWAL.get(s["id"], "pemadaman"),
             s["pagu_miliar"], s["realisasi_miliar"], deskripsi, s["warna"]),
        )
    conn.executemany(
        "INSERT INTO pos_kas (jenis, nama, sumber, nilai_triliun) VALUES (?, ?, ?, ?)",
        POS_KAS_AWAL,
    )


# ------------------------------------------------------------ baca data anggaran
def sektor_ke_dict(row):
    return {
        "id": row["id"],
        "nama": row["nama"],
        "kelompok": row["kelompok"],
        "pagu_miliar": row["pagu_miliar"],
        "realisasi_miliar": row["realisasi_miliar"],
        "realisasi_triliun": round(row["realisasi_miliar"] / 1000, 4),
        "persentase": hitung_persen(row["realisasi_miliar"], row["pagu_miliar"]),
        "deskripsi": row["deskripsi"],
        "keterangan": f"Pagu: {rupiah_miliar(row['pagu_miliar'])} • {row['deskripsi']}",
        "warna": row["warna"],
    }


def ambil_anggaran(conn):
    """Bentuknya sama dengan anggaran.json lama, ditambah beberapa kolom baru
    (angka mentah untuk form admin dan pos_kas untuk Buku Kas)."""
    r = conn.execute("SELECT * FROM ringkasan WHERE id = 1").fetchone()
    nilai = {
        "pendapatan_triliun": r["pendapatan_triliun"] if r else 0,
        "belanja_triliun": r["belanja_triliun"] if r else 0,
        "silpa_triliun": r["silpa_triliun"] if r else 0,
    }
    pos_kas = {"masuk": [], "keluar": []}
    for p in conn.execute("SELECT * FROM pos_kas ORDER BY id"):
        pos_kas[p["jenis"]].append({
            "id": p["id"],
            "nama": p["nama"],
            "sumber": p["sumber"],
            "nilai_triliun": p["nilai_triliun"],
            "nilai": rupiah_triliun(p["nilai_triliun"]),
        })
    return {
        "ringkasan": {k: rupiah_triliun(v) for k, v in nilai.items()},
        "ringkasan_nilai": nilai,
        "sektor_penyerapan": [
            sektor_ke_dict(s) for s in conn.execute("SELECT * FROM sektor ORDER BY urutan, id")
        ],
        "pos_kas": pos_kas,
    }
