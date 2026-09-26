/**
 * email-popup.js — Popup template email & WhatsApp ISB untuk satu lead komponen.
 *
 * Aplikasi TIDAK mengirim apa pun: popup ini menyiapkan penerima, subject, dan isi
 * yang sudah terisi dari "Template Email ISB.docx" supaya tinggal disalin ke aplikasi
 * email. Setelah terkirim, tombol "Tandai ..." mencatat status + jadwal follow-up.
 */
const EmailPopup = (() => {
  let data = null, kunci = null, tabAktif = 'pembuka', modal = null, onBerubah = null;
  // Endpoint per menu. Bawaan = Leads Komponen; Database Leads (klien website)
  // memanggil buka() dengan opsi sendiri dan tanpa lampiran proposal.
  const BAWAAN = { pesanUrl: '/komponen/email/', statusUrl: '/komponen/status', lampiran: true };
  let opsi = BAWAAN;

  const TANDAI = {
    pembuka: { status: 'Email Terkirim', label: 'Tandai Email Terkirim', ikon: 'envelope-check' },
    fu1: { status: 'Follow-up 1', label: 'Tandai Follow-up H+3 Terkirim', ikon: 'reply' },
    fu2: { status: 'Follow-up 2', label: 'Tandai Follow-up H+7 Terkirim', ikon: 'reply-all' },
    wa: { status: 'WA Terkirim', label: 'Tandai WA Terkirim', ikon: 'whatsapp' },
  };

  /** Salin teks; cadangan execCommand untuk akses lewat IP LAN (bukan localhost). */
  async function salin(teks, tombol) {
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(teks);
        ok = true;
      }
    } catch (e) { /* jatuh ke cadangan */ }
    if (!ok) {
      const ta = document.createElement('textarea');
      ta.value = teks;
      ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
    }
    if (tombol) {
      const asli = tombol.innerHTML;
      tombol.innerHTML = ok ? '<i class="bi bi-check2"></i> Tersalin' : 'Gagal — salin manual';
      tombol.classList.toggle('btn-success', ok);
      setTimeout(() => { tombol.innerHTML = asli; tombol.classList.remove('btn-success'); }, 1400);
    }
  }

  function el(id) { return document.getElementById(id); }

  function renderPenerima() {
    const box = el('emPenerima');
    if (!data.emails.length) {
      box.innerHTML = '<span class="text-muted">Tidak ada email — pakai tab WhatsApp.</span>';
      return;
    }
    box.innerHTML = data.emails.map((e, i) => `
      <span class="badge ${i === 0 ? 'bg-primary' : 'bg-secondary'} me-1 mb-1 em-salin" role="button"
            title="Klik untuk salin" data-teks="${escapeHtml(e)}">
        <i class="bi bi-clipboard me-1"></i>${escapeHtml(e)}</span>`).join('')
      + (data.emails.length > 1
        ? `<button class="btn btn-link btn-sm p-0 ms-1 em-salin" data-teks="${escapeHtml(data.emails.join(', '))}">salin semua</button>`
        : '');
    box.querySelectorAll('.em-salin').forEach(b => b.onclick = () => salin(b.dataset.teks, b));
  }

  function renderLampiran() {
    const box = el('emLampiran');
    const bagian = [];
    if (data.proposal_url) {
      bagian.push(`<a class="btn btn-outline-danger btn-sm" href="${data.proposal_url}">
        <i class="bi bi-file-earmark-pdf me-1"></i>Proposal ${escapeHtml(data.proposal_nomor)}</a>`);
    } else {
      bagian.push(`<button class="btn btn-outline-danger btn-sm" id="emBuatProposal">
        <i class="bi bi-file-earmark-plus me-1"></i>Buat Proposal dulu</button>`);
    }
    if (data.company_profile) {
      bagian.push(`<a class="btn btn-outline-secondary btn-sm" href="${data.company_profile}">
        <i class="bi bi-building me-1"></i>Company Profile</a>`);
    }
    box.innerHTML = bagian.join(' ');
    const b = el('emBuatProposal');
    if (b) b.onclick = async () => {
      b.disabled = true;
      b.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Membuat...';
      const hasil = await Proposal.buat(kunci);
      if (hasil && hasil.ok) buka(kunci, onBerubah);
      else { b.disabled = false; b.innerHTML = 'Coba lagi'; }
    };
  }

  function renderTab() {
    const b = data.bagian[tabAktif];
    document.querySelectorAll('#emTabs .nav-link').forEach(a =>
      a.classList.toggle('active', a.dataset.tab === tabAktif));
    const wa = tabAktif === 'wa';
    el('emCatatan').textContent = b.catatan || '';
    el('emCatatan').classList.toggle('d-none', !b.catatan);
    el('emSubjectBaris').classList.toggle('d-none', wa);
    el('emSubject').value = b.subject || '';
    el('emIsi').value = b.isi || '';
    el('emSisa').innerHTML = (b.sisa && b.sisa.length)
      ? `<i class="bi bi-exclamation-triangle me-1"></i>Masih ada bagian yang belum terisi: ${b.sisa.map(escapeHtml).join(', ')}`
      : '';
    el('emWaBaris').classList.toggle('d-none', !wa);
    el('emWaNomor').textContent = data.nomor_wa || 'tidak ada nomor WA';
    el('emWaBuka').classList.toggle('disabled', !data.wa_link);
    const t = TANDAI[tabAktif];
    el('emTandai').innerHTML = `<i class="bi bi-${t.ikon} me-1"></i>${t.label}`;
    el('emMailto').classList.toggle('d-none', wa || !data.emails.length);
  }

  function isiAkhir() { return el('emIsi').value; }

  function pasangModal() {
    if (el('emailModal')) return;
    document.body.insertAdjacentHTML('beforeend', `
<div class="modal fade" id="emailModal" tabindex="-1">
  <div class="modal-dialog modal-xl modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header text-white" style="background:#0b7285">
        <h5 class="modal-title"><i class="bi bi-envelope-paper me-2"></i><span id="emJudul"></span></h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div class="row g-3 mb-3">
          <div class="col-md-6">
            <div class="form-label mb-1 fw-semibold">Kepada</div>
            <div id="emPenerima"></div>
          </div>
          <div class="col-md-3">
            <label class="form-label mb-1 fw-semibold" for="emPic">Nama PIC (opsional)</label>
            <div class="input-group input-group-sm">
              <input id="emPic" class="form-control" placeholder="mis. Ibu Rina">
              <button class="btn btn-outline-primary" id="emPicSimpan" title="Simpan & isi ulang template">
                <i class="bi bi-arrow-repeat"></i></button>
            </div>
            <div class="form-text" style="font-size:.7rem">Menggantikan sapaan "Bapak/Ibu".</div>
          </div>
          <div class="col-md-3" id="emKolomLampiran">
            <div class="form-label mb-1 fw-semibold">Lampiran</div>
            <div id="emLampiran" class="d-flex flex-wrap gap-1"></div>
            <div class="form-text" style="font-size:.7rem">Lampirkan manual di aplikasi email (total &lt; 5 MB).</div>
          </div>
        </div>
        <ul class="nav nav-tabs mb-3" id="emTabs">
          <li class="nav-item"><a class="nav-link" href="#" data-tab="pembuka">Email Pembuka</a></li>
          <li class="nav-item"><a class="nav-link" href="#" data-tab="fu1">Follow-up H+3</a></li>
          <li class="nav-item"><a class="nav-link" href="#" data-tab="fu2">Follow-up H+7</a></li>
          <li class="nav-item"><a class="nav-link" href="#" data-tab="wa"><i class="bi bi-whatsapp text-success"></i> WhatsApp</a></li>
        </ul>
        <div id="emCatatan" class="alert alert-secondary py-1 px-2 fst-italic" style="font-size:.8rem"></div>
        <div id="emSubjectBaris" class="mb-2">
          <label class="form-label mb-1 fw-semibold">Subject</label>
          <div class="input-group">
            <input id="emSubject" class="form-control">
            <button class="btn btn-outline-primary" id="emSalinSubject"><i class="bi bi-clipboard"></i> Salin</button>
          </div>
        </div>
        <div id="emWaBaris" class="mb-2 d-flex align-items-center gap-2">
          <span class="fw-semibold">Nomor WA:</span> <code id="emWaNomor"></code>
          <button class="btn btn-outline-secondary btn-sm" id="emSalinNomor"><i class="bi bi-clipboard"></i></button>
          <a class="btn btn-success btn-sm" id="emWaBuka" target="_blank">
            <i class="bi bi-whatsapp me-1"></i>Buka WhatsApp dengan pesan ini</a>
        </div>
        <label class="form-label mb-1 fw-semibold d-flex justify-content-between">
          <span>Isi pesan <small class="text-muted fw-normal">(boleh diedit sebelum disalin)</small></span>
          <button class="btn btn-primary btn-sm" id="emSalinIsi"><i class="bi bi-clipboard me-1"></i>Salin isi</button>
        </label>
        <textarea id="emIsi" class="form-control font-monospace" rows="16" style="font-size:.85rem"></textarea>
        <div id="emSisa" class="text-danger small mt-1"></div>
      </div>
      <div class="modal-footer justify-content-between">
        <div class="d-flex gap-2">
          <a class="btn btn-outline-secondary btn-sm" id="emMailto" target="_blank"
             title="Buka aplikasi email bawaan dengan penerima, subject & isi terisi (lampiran tetap manual)">
            <i class="bi bi-box-arrow-up-right me-1"></i>Buka di aplikasi email</a>
          <span id="emStatus" class="small text-muted align-self-center"></span>
        </div>
        <button class="btn btn-success" id="emTandai"></button>
      </div>
    </div>
  </div>
</div>`);
    document.querySelectorAll('#emTabs .nav-link').forEach(a => a.onclick = ev => {
      ev.preventDefault(); tabAktif = a.dataset.tab; renderTab();
    });
    el('emSalinSubject').onclick = e => salin(el('emSubject').value, e.currentTarget);
    el('emSalinIsi').onclick = e => salin(isiAkhir(), e.currentTarget);
    el('emSalinNomor').onclick = e => salin(data.nomor_wa || '', e.currentTarget);
    el('emWaBuka').onclick = e => {
      if (!data.wa_link) { e.preventDefault(); return; }
      e.currentTarget.href = data.wa_link.split('?')[0] + '?text=' + encodeURIComponent(isiAkhir());
    };
    el('emMailto').onclick = e => {
      e.currentTarget.href = 'mailto:' + encodeURIComponent(data.emails[0] || '') +
        '?subject=' + encodeURIComponent(el('emSubject').value) +
        '&body=' + encodeURIComponent(isiAkhir());
    };
    el('emPicSimpan').onclick = async () => {
      await simpanCrm({ nama_pic: el('emPic').value });
      buka(kunci, onBerubah);
    };
    el('emTandai').onclick = async () => {
      const t = TANDAI[tabAktif];
      const d = await simpanCrm({ status: t.status });
      if (d && d.ok) {
        el('emStatus').innerHTML = `<span class="text-success"><i class="bi bi-check-circle"></i>
          Status: <strong>${escapeHtml(d.status)}</strong>${d.tanggal_follow_up ? ' · follow-up ' + escapeHtml(d.tanggal_follow_up) : ''}</span>`;
      }
    };
  }

  async function simpanCrm(isi) {
    const r = await fetch(opsi.statusUrl, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ place_key: kunci }, isi))
    });
    const d = await r.json();
    if (d.ok && onBerubah) onBerubah(kunci, d);
    return d;
  }

  async function buka(placeKey, callback, pilihan) {
    kunci = placeKey; onBerubah = callback;
    if (pilihan) opsi = Object.assign({}, BAWAAN, pilihan);
    pasangModal();
    modal = modal || new bootstrap.Modal(el('emailModal'));
    el('emStatus').textContent = '';
    el('emKolomLampiran').classList.toggle('d-none', !opsi.lampiran);
    const r = await fetch(opsi.pesanUrl + encodeURIComponent(placeKey));
    const d = await r.json();
    if (!d.ok) { alert(d.error || 'Gagal memuat template.'); return; }
    data = d;
    el('emJudul').textContent = d.nama;
    el('emPic').value = d.nama_pic || '';
    renderPenerima();
    if (opsi.lampiran) renderLampiran();
    // Tab awal mengikuti tahap lead: sudah email → follow-up berikutnya.
    tabAktif = !d.emails.length ? 'wa'
      : d.status === 'Email Terkirim' ? 'fu1'
      : d.status === 'Follow-up 1' ? 'fu2' : 'pembuka';
    renderTab();
    modal.show();
  }

  return { buka, salin };
})();
