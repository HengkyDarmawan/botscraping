/*
 * dokumen.js — Form "Buat Dokumen".
 *
 * Tiga hal yang dijaga di sini:
 *
 *  1. Kolom wajib per jenis dokumen datang dari server (window.DOK_JENIS,
 *     dirakit dari dokumen_buat.kolom_wajib). Tanda * dan kolom yang muncul
 *     TIDAK ditulis ulang di JS, supaya daftar wajib di layar tidak pernah
 *     berbeda dengan yang ditolak server.
 *  2. Rekap total dihitung di SERVER (/dokumen/hitung), bukan di browser.
 *     Angka yang terlihat di layar harus angka yang sama dengan yang masuk
 *     PDF — dua implementasi pembulatan berarti dua jawaban.
 *  3. Nilai apa pun dari katalog/lead diperlakukan sebagai teks, selalu
 *     lewat escapeHtml (didefinisikan global di progress.js).
 */
(() => {
  const JENIS = window.DOK_JENIS || [];
  const el = (id) => document.getElementById(id);
  const tbody = document.querySelector('#tabelItem tbody');

  const KOLOM_TEKS = ['pelanggan_nama', 'pelanggan_alamat', 'pelanggan_kota',
    'pelanggan_pic', 'pelanggan_jabatan', 'pelanggan_telepon', 'pelanggan_email',
    'pelanggan_npwp', 'perihal', 'referensi', 'po_nomor', 'alamat_kirim',
    'ekspedisi', 'kendaraan', 'pengemudi', 'penerima_nama', 'penerima_jabatan',
    'acuan', 'peruntukan', 'invoice_nomor', 'cara_bayar', 'termin',
    'waktu_kerja', 'garansi', 'catatan'];
  const KOLOM_TANGGAL = ['tanggal', 'jatuh_tempo', 'tanggal_referensi'];
  const KOLOM_UANG = ['biaya_kirim', 'uang_muka', 'jumlah_terima'];

  function spek() {
    return JENIS.find((j) => j.kunci === el('jenis').value) || JENIS[0];
  }

  // ─── Baris item ───────────────────────────────────────────────────────────
  // Kotak deskripsi tumbuh mengikuti isinya. Dengan tinggi tetap, spesifikasi
  // dua baris terpotong di bagian atas dan user tidak bisa membaca ulang apa
  // yang sudah diketiknya.
  function tumbuhkan(ta) {
    ta.style.height = 'auto';
    ta.style.height = `${ta.scrollHeight + 2}px`;
  }

  function barisBaru(data) {
    const d = data || {};
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="text-center text-muted small nomor-baris"></td>
      <td><textarea class="form-control form-control-sm f-deskripsi" rows="1"
             placeholder="Nama barang / jasa"></textarea></td>
      <td><input type="number" class="form-control form-control-sm f-qty" min="1" value="1"></td>
      <td><input type="text" class="form-control form-control-sm f-satuan" placeholder="unit"></td>
      <td class="kolom-harga"><input type="text" class="form-control form-control-sm f-harga" placeholder="Rp"></td>
      <td class="kolom-harga"><input type="number" class="form-control form-control-sm f-diskon" min="0" max="100"></td>
      <td class="kolom-barang d-none"><input type="text" class="form-control form-control-sm f-keterangan"></td>
      <td class="text-end align-middle small kolom-harga f-jumlah text-muted">—</td>
      <td class="text-center align-middle">
        <button type="button" class="btn btn-sm btn-link text-danger p-0 f-hapus" title="Hapus baris">
          <i class="bi bi-x-lg"></i></button>
      </td>`;
    tr.querySelector('.f-deskripsi').value = d.deskripsi || '';
    if (d.qty) tr.querySelector('.f-qty').value = d.qty;
    tr.querySelector('.f-satuan').value = d.satuan || '';
    tr.querySelector('.f-harga').value = d.harga != null ? d.harga : '';
    if (d.diskon_persen) tr.querySelector('.f-diskon').value = d.diskon_persen;
    tr.querySelector('.f-keterangan').value = d.keterangan || '';
    tr.dataset.syarat = JSON.stringify(d.syarat_bawaan || []);
    tr.querySelector('.f-hapus').addEventListener('click', () => {
      tr.remove();
      if (!tbody.children.length) barisBaru();
      segarkan();
    });
    tr.querySelectorAll('input, textarea').forEach((i) => {
      i.addEventListener('input', segarkan);
    });
    const desk = tr.querySelector('.f-deskripsi');
    desk.addEventListener('input', () => tumbuhkan(desk));
    tbody.appendChild(tr);
    tumbuhkan(desk);
    terapkanKolom();
    return tr;
  }

  function bacaItem() {
    return [...tbody.children].map((tr) => ({
      deskripsi: tr.querySelector('.f-deskripsi').value.trim(),
      qty: tr.querySelector('.f-qty').value,
      satuan: tr.querySelector('.f-satuan').value.trim(),
      harga: tr.querySelector('.f-harga').value,
      diskon_persen: tr.querySelector('.f-diskon').value,
      keterangan: tr.querySelector('.f-keterangan').value.trim(),
      syarat_bawaan: JSON.parse(tr.dataset.syarat || '[]'),
    })).filter((i) => i.deskripsi);
  }

  // ─── Visibilitas kolom per jenis ──────────────────────────────────────────
  function terapkanKolom() {
    const s = spek();
    const pakaiHarga = s.tabel === 'harga';
    document.querySelectorAll('.kolom-harga').forEach((n) => {
      n.classList.toggle('d-none', !pakaiHarga);
    });
    document.querySelectorAll('.kolom-barang').forEach((n) => {
      n.classList.toggle('d-none', s.tabel !== 'barang');
    });
    el('kartuItem').classList.toggle('d-none', s.tabel === 'tanpa');
    document.querySelectorAll('.kolom-jenis').forEach((n) => {
      n.classList.toggle('d-none', !s.wajib.includes(n.dataset.untuk));
    });
    [...tbody.children].forEach((tr, i) => {
      tr.querySelector('.nomor-baris').textContent = i + 1;
    });
  }

  // ─── Rekap dari server ────────────────────────────────────────────────────
  let jedaRekap = null;

  function segarkan() {
    terapkanKolom();
    tbody.querySelectorAll('.f-deskripsi').forEach(tumbuhkan);
    clearTimeout(jedaRekap);
    jedaRekap = setTimeout(hitung, 250);
  }

  async function hitung() {
    const s = spek();
    if (s.tabel !== 'harga') {
      el('isiRekap').innerHTML = '<span class="text-muted small">Jenis dokumen ini '
        + 'tidak memuat harga.</span>';
      el('terbilang').textContent = '';
      return;
    }
    const item = bacaItem();
    if (!item.length) {
      el('isiRekap').innerHTML = '<span class="text-muted small">Isi rincian item dulu.</span>';
      el('terbilang').textContent = '';
      return;
    }
    const muatan = {
      item,
      ppn_mode: el('ppn_mode').value,
      diskon_persen: el('diskon_persen').value,
      biaya_kirim: el('biaya_kirim').value,
      uang_muka: el('uang_muka').value,
    };
    try {
      const r = await fetch('/dokumen/hitung', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(muatan),
      });
      const d = await r.json();
      if (!d.ok) {
        el('isiRekap').innerHTML = `<span class="text-danger small">${escapeHtml(d.error)}</span>`;
        el('terbilang').textContent = '';
        return;
      }
      [...tbody.children].forEach((tr, i) => {
        const sel = tr.querySelector('.f-jumlah');
        if (sel) sel.textContent = d.baris[i] ? d.baris[i].teks : '—';
      });
      el('isiRekap').innerHTML = d.rekap.map((b) => {
        const total = b.label === 'TOTAL' || b.label === 'SISA TAGIHAN';
        return `<div class="rekap-baris${total ? ' total' : ''}">
          <span>${escapeHtml(b.label)}</span><span>${escapeHtml(b.teks)}</span></div>`;
      }).join('');
      el('terbilang').textContent = `Terbilang: ${d.terbilang}`;
    } catch (e) {
      el('isiRekap').innerHTML = '<span class="text-danger small">Server tidak '
        + 'merespons — coba lagi.</span>';
    }
  }

  // ─── Autocomplete pelanggan ───────────────────────────────────────────────
  let jedaCari = null;
  const kotakSaran = el('saranPelanggan');

  function tutupSaran() { kotakSaran.style.display = 'none'; }

  el('pelanggan_nama').addEventListener('input', () => {
    clearTimeout(jedaCari);
    const q = el('pelanggan_nama').value.trim();
    if (q.length < 2) { tutupSaran(); return; }
    jedaCari = setTimeout(async () => {
      try {
        const d = await (await fetch('/dokumen/cari-pelanggan?q=' + encodeURIComponent(q))).json();
        if (!d.ok || !d.hasil.length) { tutupSaran(); return; }
        kotakSaran.innerHTML = d.hasil.map((h, i) => `
          <button type="button" class="list-group-item list-group-item-action py-1" data-i="${i}">
            <div class="fw-semibold small">${escapeHtml(h.nama)}</div>
            <div class="text-muted" style="font-size:.72rem">
              ${escapeHtml(h.sumber)}${h.pelanggan_kota ? ' · ' + escapeHtml(h.pelanggan_kota) : ''}
            </div>
          </button>`).join('');
        kotakSaran.querySelectorAll('button').forEach((b) => {
          b.addEventListener('click', () => {
            const h = d.hasil[+b.dataset.i];
            ['pelanggan_nama', 'pelanggan_alamat', 'pelanggan_kota', 'pelanggan_pic',
              'pelanggan_jabatan', 'pelanggan_telepon', 'pelanggan_email',
              'pelanggan_npwp'].forEach((k) => {
              if (el(k) && h[k]) el(k).value = h[k];
            });
            el('place_key').value = h.place_key || '';
            el('ruang').value = h.ruang || '';
            tutupSaran();
          });
        });
        kotakSaran.style.display = 'block';
      } catch (e) { tutupSaran(); }
    }, 250);
  });
  document.addEventListener('click', (ev) => {
    if (!kotakSaran.contains(ev.target) && ev.target !== el('pelanggan_nama')) tutupSaran();
  });

  // ─── Katalog produk ───────────────────────────────────────────────────────
  let modalProduk = null;

  async function muatProduk() {
    const q = encodeURIComponent(el('cariProduk').value.trim());
    const kat = encodeURIComponent(el('filterKategori').value);
    const kotak = el('hasilProduk');
    try {
      const d = await (await fetch(`/dokumen/produk/cari?q=${q}&kategori=${kat}`)).json();
      if (!d.ok || !d.hasil.length) {
        kotak.innerHTML = '<div class="text-muted small p-2">Tidak ada produk yang cocok.</div>';
        return;
      }
      kotak.innerHTML = d.hasil.map((p, i) => `
        <button type="button" class="list-group-item list-group-item-action" data-i="${i}">
          <div class="d-flex justify-content-between gap-2">
            <div>
              <div class="fw-semibold small">${escapeHtml(p.nama)}</div>
              <div class="text-muted" style="font-size:.72rem">
                ${escapeHtml(p.kategori || '-')}${p.satuan ? ' · per ' + escapeHtml(p.satuan) : ''}
                ${p.siklus && p.siklus !== 'sekali' ? ' · ' + escapeHtml(p.siklus) : ''}
              </div>
            </div>
            <div class="text-nowrap small">Rp ${escapeHtml(String(p.harga).replace(/\B(?=(\d{3})+(?!\d))/g, '.'))}</div>
          </div>
        </button>`).join('');
      kotak.querySelectorAll('button').forEach((b) => {
        b.addEventListener('click', () => {
          const p = d.hasil[+b.dataset.i];
          tambahProduk(p);
          segarkan();
        });
      });
    } catch (e) {
      kotak.innerHTML = '<div class="text-danger small p-2">Gagal memuat katalog.</div>';
    }
  }

  function tambahProduk(p) {
    const kosong = [...tbody.children].find(
      (tr) => !tr.querySelector('.f-deskripsi').value.trim());
    if (kosong) kosong.remove();
    barisBaru({
      deskripsi: p.deskripsi ? `${p.nama} — ${p.deskripsi}` : p.nama,
      qty: 1, satuan: p.satuan, harga: p.harga,
      syarat_bawaan: p.syarat_bawaan || [],
    });
  }

  el('btnCariProduk').addEventListener('click', () => {
    modalProduk = modalProduk || new bootstrap.Modal(el('modalProduk'));
    modalProduk.show();
    muatProduk();
  });
  el('cariProduk').addEventListener('input', () => {
    clearTimeout(jedaCari);
    jedaCari = setTimeout(muatProduk, 250);
  });
  el('filterKategori').addEventListener('change', muatProduk);
  document.querySelectorAll('.btn-paket').forEach((b) => {
    b.addEventListener('click', () => {
      (JSON.parse(b.dataset.items || '[]')).forEach(tambahProduk);
      segarkan();
    });
  });

  // ─── Kirim ────────────────────────────────────────────────────────────────
  function kumpulkan() {
    const d = { jenis: el('jenis').value, induk_id: el('induk_id').value || null };
    KOLOM_TEKS.concat(KOLOM_TANGGAL, KOLOM_UANG).forEach((k) => {
      if (el(k)) d[k] = el(k).value.trim();
    });
    d.ppn_mode = el('ppn_mode').value;
    d.diskon_persen = el('diskon_persen').value;
    d.place_key = el('place_key').value;
    d.ruang = el('ruang').value;
    d.lampiran = el('lampiran').value.split(',').map((s) => s.trim()).filter(Boolean);
    d.item = spek().tabel === 'tanpa' ? [] : bacaItem();
    return d;
  }

  el('btnBuat').addEventListener('click', async () => {
    const btn = el('btnBuat');
    const pesan = el('pesanForm');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Membuat...';
    pesan.innerHTML = '';
    try {
      const r = await fetch('/dokumen/buat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(kumpulkan()),
      });
      const d = await r.json();
      if (!d.ok) {
        pesan.innerHTML = `<div class="alert alert-danger py-2 small mb-0">
          ${escapeHtml(d.error)}
          ${d.pengaturan ? ' <a href="/dokumen" class="alert-link">Buka Pengaturan</a>' : ''}
        </div>`;
        return;
      }
      pesan.innerHTML = `<div class="alert alert-success py-2 small mb-0">
        <div class="fw-semibold mb-1">${escapeHtml(d.jenis)} ${escapeHtml(d.nomor)} siap.</div>
        <div class="d-flex gap-2 flex-wrap">
          ${d.pdf ? `<a href="${d.pdf}" class="btn btn-sm btn-danger">
            <i class="bi bi-file-earmark-pdf me-1"></i>Unduh PDF</a>
            <a href="${d.pdf}?lihat=1" target="_blank" class="btn btn-sm btn-outline-danger">Lihat</a>` : ''}
          <a href="${d.docx}" class="btn btn-sm btn-outline-primary">
            <i class="bi bi-file-earmark-word me-1"></i>Word</a>
        </div>
        ${d.peringatan ? `<div class="mt-2 text-warning">${escapeHtml(d.peringatan)}</div>` : ''}
      </div>`;
      el('induk_id').value = '';
    } catch (e) {
      pesan.innerHTML = '<div class="alert alert-danger py-2 small mb-0">Server tidak '
        + 'merespons. Pastikan <code>python app.py</code> masih jalan.</div>';
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-file-earmark-pdf me-1"></i>Buat Dokumen (PDF + Word)';
    }
  });

  // ─── Awal ─────────────────────────────────────────────────────────────────
  el('jenis').addEventListener('change', segarkan);
  ['ppn_mode', 'diskon_persen', 'biaya_kirim', 'uang_muka'].forEach((k) => {
    el(k).addEventListener('input', segarkan);
    el(k).addEventListener('change', segarkan);
  });
  el('btnBarisBaru').addEventListener('click', () => { barisBaru(); segarkan(); });

  // Item awal hanya ada saat Duplikat/Revisi; di luar itu satu baris kosong.
  const awalItem = window.DOK_AWAL_ITEM || [];
  if (awalItem.length) awalItem.forEach(barisBaru);
  else barisBaru();
  segarkan();
})();
