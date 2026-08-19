/* pricing.js — logika halaman Price Comparison.
 *
 * Dipindahkan dari <script> inline di templates/pricing.html supaya bisa
 * bertambah tanpa membuat templatenya jadi tidak terbaca.
 *
 * Dua bagian:
 *   1. Panen  — konfigurasi + tombol scraping (memakai startJob dari progress.js)
 *   2. Cek Harga — tanya server dari data yang SUDAH dipanen, tanpa browser.
 *      Mengubah modal atau persentase biaya tidak boleh memicu scraping ulang;
 *      user akan mengutak-atik angka itu berkali-kali.
 */

/* ── Util ── */

function rupiah(n) {
  if (n === null || n === undefined || n === '' || isNaN(n)) return '-';
  return 'Rp ' + Math.round(n).toLocaleString('id-ID');
}

function angkaDari(id) {
  const v = (document.getElementById(id).value || '').replace(/[^\d,.-]/g, '')
    .replace(/\./g, '').replace(',', '.');
  return v === '' ? null : parseFloat(v);
}

/* ── Konfigurasi panen ── */

function switchMode(mode) {
  const showStores = (mode === 'toko' || mode === 'keyword_toko');
  document.getElementById('sectionProduk').style.display = mode === 'produk' ? 'block' : 'none';
  document.getElementById('sectionToko').style.display = showStores ? 'block' : 'none';
  document.getElementById('sectionKwToko').style.display = mode === 'keyword_toko' ? 'block' : 'none';
  if (showStores) loadStores();
}

function toggleProxy(cb) {
  document.getElementById('proxySection').style.display = cb.checked ? 'block' : 'none';
}

function switchEngine(engine) {
  document.getElementById('chromePanel').style.display = engine === 'my_chrome' ? 'block' : 'none';
  if (engine === 'my_chrome') checkChromeStatus();
}

function launchChrome(btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Membuka...';
  fetch('/pricing/launch-chrome', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
    .then(r => r.json()).then(d => {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-box-arrow-up-right me-1"></i>Buka Chrome Scraping';
      if (!d.ok) { alert(d.error || 'Gagal membuka Chrome'); return; }
      setTimeout(checkChromeStatus, 2500);
    }).catch(() => { btn.disabled = false; });
}

function checkChromeStatus() {
  const el = document.getElementById('chromeStatus');
  el.className = 'badge bg-secondary'; el.textContent = 'Status: mengecek...';
  fetch('/pricing/chrome-status').then(r => r.json()).then(d => {
    if (d.connected) { el.className = 'badge bg-success'; el.textContent = '✓ Terhubung (port ' + d.port + ')'; }
    else { el.className = 'badge bg-danger'; el.textContent = '✗ Belum terhubung — klik "Buka Chrome Scraping"'; }
  });
}

/* ── Database toko ── */

let STORES = [];

function loadStores() {
  fetch('/pricing/stores').then(r => r.json()).then(data => {
    STORES = data.stores || [];
    const el = document.getElementById('storeList');
    if (!el) return;
    if (!STORES.length) {
      el.innerHTML = '<div class="text-muted small">Belum ada toko. Tambah di bawah.</div>';
      return;
    }
    let html = '';
    ['tokopedia', 'shopee'].forEach(plat => {
      const group = STORES.filter(s => s.platform === plat);
      if (!group.length) return;
      html += `<div class="fw-bold small text-uppercase text-muted mt-1">${plat === 'tokopedia' ? '🟢 Tokopedia' : '🛒 Shopee'}</div>`;
      group.forEach(s => {
        // Penanda toko sendiri ditampilkan sebagai badge yang bisa diklik:
        // penandaan inilah yang menentukan baris mana dikeluarkan dari
        // statistik pasar, jadi harus terlihat dan bisa dibetulkan sekali klik.
        const own = s.is_own ? 1 : 0;
        const badge = own
          ? '<span class="badge bg-primary ms-1" style="font-size:.6rem">TOKO SAYA</span>'
          : '<span class="badge bg-light text-muted ms-1" style="font-size:.6rem">pesaing</span>';
        html += `<div class="form-check d-flex align-items-center">
          <input class="form-check-input store-cb" type="checkbox" value="${s.id}" id="cb_${s.id}">
          <label class="form-check-label flex-grow-1 ms-1" for="cb_${s.id}">${escapeHtml(s.nama)}
            <a href="${s.url}" target="_blank" class="text-muted ms-1" style="font-size:.7rem">↗</a></label>
          <button class="btn btn-sm py-0 px-1 border-0" type="button"
                  title="Klik untuk menandai toko sendiri / pesaing"
                  onclick="toggleOwn('${s.id}', ${own ? 0 : 1})">${badge}</button>
          <button class="btn btn-sm text-danger py-0 px-1" type="button" title="Hapus"
                  onclick="delStore('${s.id}')"><i class="bi bi-trash"></i></button>
        </div>`;
      });
    });
    el.innerHTML = html;
  });
}

function toggleOwn(id, isOwn) {
  fetch('/pricing/stores/own', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id, is_own: !!isOwn})
  }).then(r => r.json()).then(() => loadStores());
}

function addStore() {
  const platform = document.getElementById('newStorePlatform').value;
  const nama = document.getElementById('newStoreNama').value.trim();
  const url = document.getElementById('newStoreUrl').value.trim();
  const isOwn = document.getElementById('newStoreOwn').checked;
  const st = document.getElementById('addStoreStatus');
  if (!url) { st.innerHTML = '<span class="text-danger">URL / username wajib diisi.</span>'; return; }
  fetch('/pricing/stores', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({platform, nama, url, is_own: isOwn})
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      st.innerHTML = '<span class="text-success">✓ Toko ditambahkan.</span>';
      document.getElementById('newStoreNama').value = '';
      document.getElementById('newStoreUrl').value = '';
      document.getElementById('newStoreOwn').checked = false;
      loadStores();
    } else { st.innerHTML = `<span class="text-danger">${escapeHtml(d.error || 'Gagal')}</span>`; }
  });
}

function delStore(id) {
  if (!confirm('Hapus toko ini beserta produk & riwayat harganya?')) return;
  fetch('/pricing/stores/delete', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id})
  }).then(r => r.json()).then(() => { loadStores(); muatStatData(); });
}

/* ── Jalankan panen ── */

function runPricing() {
  const mode = document.querySelector('input[name="mode"]:checked').value;
  const engine = document.querySelector('input[name="engine"]:checked').value;
  const params = {
    mode: mode,
    engine: engine,
    max_results: parseInt(document.getElementById('maxPriceResults').value) || 20,
    headless: !document.getElementById('showBrowser').checked,
    use_proxy: document.getElementById('useProxy').checked,
    proxy_server: document.getElementById('proxyServer').value.trim(),
    proxy_username: document.getElementById('proxyUser').value.trim(),
    proxy_password: document.getElementById('proxyPass').value.trim(),
  };

  if (mode === 'produk') {
    const kw = document.getElementById('keyword').value.trim();
    if (!kw) { alert('Masukkan keyword produk terlebih dahulu.'); return; }
    const sources = [];
    if (document.getElementById('srcTokopedia').checked) sources.push('tokopedia');
    if (document.getElementById('srcShopee').checked) sources.push('shopee');
    if (document.getElementById('srcCustom').checked) sources.push('custom');
    if (!sources.length) { alert('Pilih minimal satu sumber data.'); return; }
    Object.assign(params, {
      keyword: kw,
      sources: sources,
      custom_url: document.getElementById('customUrl').value.trim(),
      custom_sel_harga: document.getElementById('customSelHarga').value.trim(),
      custom_sel_nama: document.getElementById('customSelNama').value.trim(),
      use_gemini: document.getElementById('useGemini').checked,
      gemini_api_key: document.getElementById('geminiApiKey').value.trim(),
    });
  } else {
    const ids = Array.from(document.querySelectorAll('.store-cb:checked')).map(c => c.value);
    if (!ids.length) { alert('Pilih minimal satu toko.'); return; }
    params.store_ids = ids;
    if (mode === 'keyword_toko') {
      const kw = document.getElementById('kwToko').value.trim();
      if (!kw) { alert('Masukkan keyword produk terlebih dahulu.'); return; }
      params.keyword = kw;
    }
  }

  startJob('/pricing/run', params, muatStatData);
}

/* ── Cek Harga ── */

let CEK_KUNCI = null;

function muatStatData() {
  fetch('/pricing/data').then(r => r.json()).then(d => {
    const el = document.getElementById('cekStatProduk');
    if (!el || !d.ok) return;
    const s = d.stats;
    el.textContent = `${s.produk} produk · ${s.toko} toko`;
  }).catch(() => {});
}

/* Panen bertarget: buka browser, cari produk ini di etalase toko pembanding.
 * Sengaja terpisah dari "Cek Harga" — yang itu instan dan hanya membaca DB. */
function cariDiToko() {
  const kueri = document.getElementById('cekKueri').value.trim();
  if (!kueri) {
    document.getElementById('cekHasil').innerHTML =
      '<div class="text-danger small">Ketik dulu nama produknya.</div>';
    return;
  }
  const ids = Array.from(document.querySelectorAll('.store-cb:checked')).map(c => c.value);
  const engineEl = document.querySelector('input[name="engine"]:checked');
  const showEl = document.getElementById('showBrowser');
  startJob('/pricing/cari-toko', {
    kueri: kueri,
    store_ids: ids.length ? ids : null,
    engine: engineEl ? engineEl.value : 'camoufox',
    headless: showEl ? !showEl.checked : false,
  }, function () {
    // Job ini tidak menghasilkan file, jadi progress.js menandainya
    // "⚠ Tanpa hasil". Untuk alur ini itu keliru — hasilnya ada, cuma di DB.
    const st = document.getElementById('statusBadge');
    if (st) { st.textContent = '✅ Selesai'; st.className = 'badge bg-success'; }
    const bar = document.getElementById('progressBar');
    if (bar) bar.className = 'progress-bar bg-success';
    const run = document.getElementById('runBtn');
    if (run) run.innerHTML = '<i class="bi bi-play-fill me-2"></i>Panen Harga';
    muatStatData();
    cekHarga();
  });
}

function cekHarga(kunci) {
  const kueri = document.getElementById('cekKueri').value.trim();
  const hasil = document.getElementById('cekHasil');
  if (!kueri) { hasil.innerHTML = '<div class="text-danger small">Ketik dulu nama produknya.</div>'; return; }
  CEK_KUNCI = (typeof kunci === 'string') ? kunci : null;

  hasil.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-2"></span>Menghitung...</div>';
  fetch('/pricing/cek', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      kueri: kueri,
      kunci: CEK_KUNCI,
      modal: angkaDari('cekModal'),
      fee_persen: angkaDari('cekFee'),
      biaya_tetap: angkaDari('cekTetap'),
      margin_target: angkaDari('cekMargin'),
    })
  }).then(r => r.json()).then(renderCek)
    .catch(e => { hasil.innerHTML = `<div class="text-danger small">Gagal: ${escapeHtml(String(e))}</div>`; });
}

function renderCek(d) {
  const varianEl = document.getElementById('cekVarian');
  const el = document.getElementById('cekHasil');
  if (!d.ok) {
    varianEl.innerHTML = '';
    el.innerHTML = `<div class="alert alert-warning py-2 small mb-0">${escapeHtml(d.error || 'Gagal')}</div>`;
    return;
  }

  // ── Disambiguasi varian ──
  // "intel 7 gen 12" sah cocok ke 12700K, 12700F, dan 12700KF sekaligus.
  // Menggabungkannya jadi satu "harga pasar" akan mencampur produk yang
  // harganya terpaut ratusan ribu, jadi user memilih dulu.
  if (d.varian && d.varian.length > 1) {
    let v = '<div class="small fw-bold mb-1">Ketemu ' + d.varian.length + ' varian — pilih yang kamu maksud:</div>';
    v += '<div class="list-group list-group-flush mb-2" style="max-height:180px;overflow-y:auto">';
    d.varian.forEach(x => {
      v += `<button type="button" class="list-group-item list-group-item-action py-1 px-2 ${x.dipilih ? 'active' : ''}"
              onclick="cekHarga('${x.kunci.replace(/'/g, "\\'")}')">
              <span class="fw-bold">${escapeHtml(x.label)}</span>
              <span class="badge ${x.dipilih ? 'bg-light text-dark' : 'bg-secondary'} ms-1">${x.n_toko} toko</span>
              ${x.punya_sendiri ? '<span class="badge bg-info ms-1">ada produk kita</span>' : ''}
              <span class="float-end small">${rupiah(x.harga_min)} – ${rupiah(x.harga_maks)}</span>
            </button>`;
    });
    v += '</div>';
    varianEl.innerHTML = v;
  } else {
    varianEl.innerHTML = '';
  }

  const s = d.statistik || {};
  const kelasKeyakinan = {
    'BAIK': 'success', 'LEMAH': 'warning', 'SEBARAN LEBAR': 'warning', 'KOSONG': 'danger'
  }[d.keyakinan.tingkat] || 'secondary';

  let h = `<div class="d-flex justify-content-between align-items-center mb-2">
      <h6 class="fw-bold mb-0">${escapeHtml(d.label || d.kueri)}</h6>
      <span class="badge bg-${kelasKeyakinan}">${escapeHtml(d.keyakinan.tingkat)}</span>
    </div>
    <div class="small text-muted mb-2">${escapeHtml(d.keyakinan.alasan)}</div>`;

  if (!s.n) {
    el.innerHTML = h + (d.catatan || []).map(c =>
      `<div class="alert alert-warning py-2 small mb-0">${escapeHtml(c)}</div>`).join('');
    return;
  }

  // ── Stat tiles ──
  h += '<div class="row g-2 mb-3">';
  [['Termurah', s.min, 'success'], ['Median', s.median, 'primary'],
   ['Termahal', s.maks, 'secondary'], ['Pembanding', s.n + ' toko', 'dark']
  ].forEach(([label, nilai, warna]) => {
    const teks = (typeof nilai === 'number') ? rupiah(nilai) : nilai;
    h += `<div class="col-3"><div class="stat-card border-${warna}" style="border-left:4px solid">
            <div class="stat-label">${label}</div>
            <div class="fw-bold" style="font-size:1rem;color:#1a3a5c">${teks}</div>
          </div></div>`;
  });
  h += '</div>';

  // ── Status per toko ──
  // "Toko ini tidak menjualnya" itu jawaban yang sah, tapi hanya kalau
  // dikatakan. Kalau tokonya cuma hilang dari tabel, user tidak bisa
  // membedakannya dari panen yang gagal.
  if (d.toko_status && d.toko_status.length) {
    h += '<div class="d-flex flex-wrap gap-1 mb-3">';
    d.toko_status.forEach(t => {
      const warna = t.punya ? 'success' : (t.n_produk_tersimpan ? 'secondary' : 'warning');
      const ikon = t.punya ? 'check-circle' : (t.n_produk_tersimpan ? 'dash-circle' : 'question-circle');
      const ket = t.punya ? 'punya produk ini'
        : (t.n_produk_tersimpan ? 'tidak menjual produk ini'
                                : 'belum pernah dipanen');
      h += `<span class="badge bg-${warna}-subtle text-${warna}-emphasis border border-${warna}-subtle"
              title="${escapeHtml(ket)} · ${t.n_produk_tersimpan} produk tersimpan">
              <i class="bi bi-${ikon} me-1"></i>${escapeHtml(t.nama || '')}
              ${t.is_own ? '<span class="badge bg-primary ms-1" style="font-size:.55rem">KITA</span>' : ''}
            </span>`;
    });
    h += '</div>';
  }

  // ── Posisi harga kita ──
  if (!d.harga_kita && s.n) {
    h += `<div class="alert alert-info py-2 small mb-3">
        <i class="bi bi-lightbulb me-1"></i>
        <strong>Belum dijual di toko kamu.</strong> Angka di bawah adalah rentang
        harga masuk yang wajar kalau mau mulai menjualnya.
      </div>`;
  }
  if (d.harga_kita) {
    const v = d.verdict || d.posisi;
    h += `<div class="d-flex align-items-center gap-2 mb-3 p-2 rounded" style="background:#f6f9fc">
        <div class="harga-badge harga-${(v || '').replace(/\s+/g, '_')}">${rupiah(d.harga_kita)}</div>
        <div class="small">
          <div class="fw-bold">${escapeHtml(v || '')}</div>
          <div class="text-muted">${escapeHtml(d.peringkat || '')} ·
            ${d.selisih_median > 0 ? '+' : ''}${d.selisih_median_persen}% vs median</div>
        </div>`;
    if (d.laba_kita !== undefined) {
      h += `<div class="ms-auto text-end small">
              <div class="text-muted">laba/unit</div>
              <div class="fw-bold ${d.laba_kita < 0 ? 'text-danger' : 'text-success'}">
                ${rupiah(d.laba_kita)} (${d.margin_kita}%)</div>
            </div>`;
    }
    h += '</div>';
  }

  // ── Band rekomendasi ──
  h += '<div class="row g-2 mb-3">';
  (d.band || []).forEach(b => {
    const warna = b.nama === 'Seimbang' ? 'primary'
      : b.nama === 'Agresif' ? 'success'
      : b.nama === 'Harga Impas Minimum' ? 'danger' : 'secondary';
    const lebar = (d.band.length === 1) ? 12 : 4;
    h += `<div class="col-md-${lebar}">
      <div class="card h-100 border-${warna}">
        <div class="card-header bg-${warna} text-white py-1 px-2 small fw-bold">${escapeHtml(b.nama)}</div>
        <div class="card-body p-2">
          <div class="fw-bold mb-2" style="font-size:1.25rem;color:#1a3a5c">${escapeHtml(b.harga_tampil)}</div>
          <table class="table table-sm mb-2" style="font-size:.75rem">
            <tr><td class="text-muted ps-0">Potongan ${d.biaya.fee_persen}%</td>
                <td class="text-end pe-0 text-danger">−${rupiah(b.potongan)}</td></tr>
            <tr><td class="text-muted ps-0">Biaya tetap</td>
                <td class="text-end pe-0 text-danger">−${rupiah(b.biaya_tetap)}</td></tr>
            <tr class="border-top"><td class="ps-0 fw-bold">Uang bersih</td>
                <td class="text-end pe-0 fw-bold">${rupiah(b.uang_bersih)}</td></tr>
          </table>
          <div class="p-2 rounded" style="background:#fff8e1;font-size:.75rem">
            <div>Modal harus <strong>di bawah ${rupiah(b.modal_maks_impas)}</strong> supaya tidak rugi</div>
            <div class="text-muted">di bawah ${rupiah(b.modal_maks_margin)} untuk margin
              ${Math.round(d.biaya.margin_target * 100)}%</div>
          </div>`;
    if (b.laba !== undefined) {
      h += `<div class="mt-2 small ${b.laba < 0 ? 'text-danger' : 'text-success'}">
              Laba ${rupiah(b.laba)} (${b.margin_persen}%)</div>`;
    }
    h += `<div class="text-muted mt-2" style="font-size:.7rem">${escapeHtml(b.alasan)}</div>
        </div></div></div>`;
  });
  h += '</div>';

  // ── Catatan ──
  (d.catatan || []).forEach(c => {
    h += `<div class="alert alert-warning py-2 small">${escapeHtml(c)}</div>`;
  });

  // ── Tabel pembanding ──
  h += `<div class="fw-bold small mb-1">Harga pembanding</div>
    <div class="table-responsive"><table class="table table-sm table-hover table-results mb-0" style="font-size:.78rem">
    <thead><tr><th>Toko</th><th>Produk</th><th class="text-end">Harga</th>
    <th class="text-end">Terjual</th><th></th></tr></thead><tbody>`;
  const semua = (d.milik || []).concat(d.rival || []);
  semua.sort((a, b) => (a.harga || 0) - (b.harga || 0));
  semua.forEach(r => {
    h += `<tr class="${r.is_own ? 'table-info' : ''}">
      <td>${escapeHtml(r.toko || r.store_id || '-')}${r.is_own ? ' <span class="badge bg-primary" style="font-size:.55rem">KITA</span>' : ''}</td>
      <td class="text-truncate" style="max-width:260px" title="${escapeHtml(r.nama_produk || '')}">${escapeHtml(r.nama_produk || '')}</td>
      <td class="text-end fw-bold">${rupiah(r.harga)}</td>
      <td class="text-end text-muted">${escapeHtml(String(r.terjual ?? '-'))}</td>
      <td>${r.url ? `<a href="${r.url}" target="_blank" class="text-muted">↗</a>` : ''}</td>
    </tr>`;
  });
  h += '</tbody></table></div>';

  el.innerHTML = h;
}

/* ── Init ── */

document.addEventListener('DOMContentLoaded', function () {
  const custom = document.getElementById('srcCustom');
  if (custom) {
    custom.addEventListener('change', function () {
      document.getElementById('customUrlSection').style.display = this.checked ? 'block' : 'none';
    });
  }
  loadStores();
  muatStatData();
});
