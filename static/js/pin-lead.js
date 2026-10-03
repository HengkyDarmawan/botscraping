/**
 * pin-lead.js — PIN "lead berpotensi" di Database Leads & Leads Komponen.
 *
 * Tombol bintang per baris (.btn-pin) dan tombol massal untuk baris yang
 * dicentang. Lead yang dipin punya tab sendiri dan selalu tampil paling atas;
 * urutan baru terlihat setelah halaman dimuat ulang, jadi di sini cukup ikon &
 * warna barisnya yang berganti.
 */
const PinLead = (() => {
  let url = null;

  function tampil(tr, dipin) {
    tr.dataset.dipin = dipin ? '1' : '0';
    tr.classList.toggle('baris-dipin', dipin);
    const b = tr.querySelector('.btn-pin');
    if (!b) return;
    b.innerHTML = `<i class="bi ${dipin ? 'bi-star-fill text-warning' : 'bi-star text-muted'}"></i>`;
    b.title = dipin ? 'Lepas PIN' : 'PIN sebagai lead berpotensi';
  }

  async function kirim(keys, dipin) {
    const d = await (await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keys, dipin })
    })).json();
    if (!d.ok) { alert(d.error || 'Gagal menyimpan PIN.'); return false; }
    keys.forEach(k => {
      const tr = document.querySelector(`tr[data-key="${CSS.escape(k)}"]`);
      if (tr) tampil(tr, dipin);
    });
    return true;
  }

  /** `ambilTerpilih()` → place_key baris yang dicentang. */
  function pasang(pinUrl, ambilTerpilih) {
    url = pinUrl;
    document.querySelectorAll('tr[data-key]').forEach(tr => tampil(tr, tr.dataset.dipin === '1'));
    document.querySelectorAll('.btn-pin').forEach(b => b.addEventListener('click', () => {
      const tr = b.closest('tr[data-key]');
      kirim([tr.dataset.key], tr.dataset.dipin !== '1');
    }));
    document.querySelectorAll('[data-pin-massal]').forEach(b => b.addEventListener('click', async () => {
      const keys = ambilTerpilih();
      if (!keys.length) return;
      b.disabled = true;
      try { await kirim(keys, b.dataset.pinMassal === '1'); } finally { b.disabled = false; }
    }));
  }

  return { pasang };
})();
