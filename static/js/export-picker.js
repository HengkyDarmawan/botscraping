/**
 * export-picker.js — Modal "pilih kolom sebelum export" (Leads web & Leads Komponen).
 *
 * Pemakaian:
 *   ExportPicker.buka({
 *     kunci: 'komponen',                  // nama simpanan pilihan di localStorage
 *     katalogUrl: '/komponen/export/kolom',
 *     exportUrl: '/komponen/export',
 *     payload: () => ({ filter: {...}, keys: [...] }),   // data tambahan ke server
 *     info: () => 'Mengekspor 12 baris terpilih',
 *   });
 *
 * Pilihan kolom terakhir diingat per halaman, jadi export harian "Nama + WA"
 * cukup satu klik.
 */
const ExportPicker = (() => {
  let opsi = null, katalog = null, modal = null;
  const PRESET_LABEL = {
    nama_wa: 'Nama + WA', nama_wa_email: 'Nama + WA + Email',
    kontak: 'Kontak lengkap', lengkap: 'Semua kolom'
  };

  function simpanan(k) {
    try { return JSON.parse(localStorage.getItem('export:' + k) || 'null'); } catch (e) { return null; }
  }
  function simpan(k, v) {
    try { localStorage.setItem('export:' + k, JSON.stringify(v)); } catch (e) { /* abaikan */ }
  }

  function pastikanModal() {
    if (document.getElementById('exportModal')) return;
    document.body.insertAdjacentHTML('beforeend', `
<div class="modal fade" id="exportModal" tabindex="-1">
  <div class="modal-dialog modal-lg modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header bg-success text-white">
        <h5 class="modal-title"><i class="bi bi-file-earmark-excel me-2"></i>Export — pilih kolom</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div id="expInfo" class="alert alert-light border py-2 mb-3" style="font-size:.85rem"></div>
        <div class="mb-3">
          <div class="form-label mb-1">Pilihan cepat</div>
          <div id="expPreset" class="d-flex flex-wrap gap-1"></div>
        </div>
        <div class="row g-3" id="expKolom"></div>
        <div class="mt-3 p-2 bg-light rounded" style="font-size:.82rem">
          <div class="fw-semibold mb-1">Urutan kolom di file:</div>
          <div id="expUrutan" class="d-flex flex-wrap gap-1"></div>
        </div>
        <div class="row g-2 mt-2">
          <div class="col-md-7">
            <label class="form-label mb-1">Hanya baris yang punya</label>
            <select id="expWajib" class="form-select form-select-sm">
              <option value="">Semua baris (tanpa syarat)</option>
              <option value="wa">Nomor WhatsApp</option>
              <option value="email">Email</option>
              <option value="wa_atau_email">WhatsApp atau email</option>
              <option value="wa_dan_email">WhatsApp dan email</option>
            </select>
          </div>
          <div class="col-md-5">
            <label class="form-label mb-1">Format</label>
            <select id="expFormat" class="form-select form-select-sm">
              <option value="xlsx">Excel (.xlsx)</option>
              <option value="csv">CSV (.csv)</option>
            </select>
          </div>
        </div>
        <div id="expGalat" class="text-danger mt-2 small"></div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Batal</button>
        <button type="button" class="btn btn-success" id="expJalan">
          <i class="bi bi-download me-1"></i>Export
        </button>
      </div>
    </div>
  </div>
</div>`);
    document.getElementById('expJalan').onclick = jalankan;
  }

  function terpilih() {
    // Urutan = urutan dicentang (disimpan di data-urut), bukan urutan tampil.
    return [...document.querySelectorAll('#expKolom input:checked')]
      .sort((a, b) => (+a.dataset.urut) - (+b.dataset.urut))
      .map(i => i.value);
  }

  let penghitung = 0;
  function tandai(kolom) {
    penghitung = 0;
    document.querySelectorAll('#expKolom input').forEach(i => { i.checked = false; i.dataset.urut = ''; });
    kolom.forEach(k => {
      const el = document.querySelector(`#expKolom input[value="${CSS.escape(k)}"]`);
      if (el) { el.checked = true; el.dataset.urut = ++penghitung; }
    });
    tampilUrutan();
  }

  function tampilUrutan() {
    const label = Object.fromEntries(katalog.kolom.map(k => [k.k, k.label]));
    const pilih = terpilih();
    document.getElementById('expUrutan').innerHTML = pilih.length
      ? pilih.map((k, i) => `<span class="badge bg-success">${i + 1}. ${escapeHtml(label[k] || k)}</span>`).join('')
      : '<span class="text-danger">Belum ada kolom dipilih</span>';
  }

  function render() {
    const grup = {};
    katalog.kolom.forEach(k => (grup[k.grup] = grup[k.grup] || []).push(k));
    document.getElementById('expKolom').innerHTML = Object.entries(grup).map(([g, isi]) => `
      <div class="col-6 col-md-4">
        <div class="fw-semibold small text-muted mb-1">${escapeHtml(g)}</div>
        ${isi.map(k => `
          <div class="form-check">
            <input class="form-check-input" type="checkbox" value="${escapeHtml(k.k)}" id="exp_${escapeHtml(k.k)}">
            <label class="form-check-label small" for="exp_${escapeHtml(k.k)}">${escapeHtml(k.label)}</label>
          </div>`).join('')}
      </div>`).join('');
    document.querySelectorAll('#expKolom input').forEach(i => i.onchange = () => {
      i.dataset.urut = i.checked ? ++penghitung : '';
      tampilUrutan();
    });
    document.getElementById('expPreset').innerHTML = (katalog.urutan_preset || Object.keys(katalog.preset)).map(p =>
      `<button type="button" class="btn btn-outline-success btn-sm" data-p="${p}">${PRESET_LABEL[p] || p}</button>`
    ).join('');
    document.querySelectorAll('#expPreset button').forEach(b => b.onclick = () => {
      tandai(katalog.preset[b.dataset.p]);
      // "Nama + WA" tanpa baris yang nomornya kosong — itu yang dimaksud user.
      const wajib = { nama_wa: 'wa', nama_wa_email: 'wa_atau_email' }[b.dataset.p] || '';
      document.getElementById('expWajib').value = wajib;
    });
  }

  async function buka(o) {
    opsi = o;
    pastikanModal();
    document.getElementById('expGalat').textContent = '';
    document.getElementById('expInfo').innerHTML = o.info ? o.info() : '';
    if (!katalog || katalog._url !== o.katalogUrl) {
      katalog = await (await fetch(o.katalogUrl)).json();
      katalog._url = o.katalogUrl;
      render();
    }
    const lama = simpanan(o.kunci);
    tandai(lama && lama.kolom && lama.kolom.length ? lama.kolom : katalog.preset.nama_wa);
    document.getElementById('expWajib').value = lama ? (lama.wajib || '') : 'wa';
    document.getElementById('expFormat').value = lama ? (lama.format || 'xlsx') : 'xlsx';
    modal = modal || new bootstrap.Modal(document.getElementById('exportModal'));
    modal.show();
  }

  async function jalankan() {
    const kolom = terpilih();
    const galat = document.getElementById('expGalat');
    if (!kolom.length) { galat.textContent = 'Pilih minimal satu kolom.'; return; }
    const wajib = document.getElementById('expWajib').value;
    const format = document.getElementById('expFormat').value;
    simpan(opsi.kunci, { kolom, wajib, format });
    const btn = document.getElementById('expJalan');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Menyiapkan...';
    try {
      const r = await fetch(opsi.exportUrl, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({ kolom, wajib, format }, opsi.payload ? opsi.payload() : {}))
      });
      const d = await r.json();
      if (!d.ok) { galat.textContent = d.error || 'Export gagal.'; return; }
      window.location = d.url;
      galat.innerHTML = `<span class="text-success">${d.jumlah} baris diekspor.</span>`;
      setTimeout(() => modal.hide(), 800);
    } catch (e) {
      galat.textContent = 'Tidak bisa menghubungi server.';
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-download me-1"></i>Export';
    }
  }

  return { buka };
})();
