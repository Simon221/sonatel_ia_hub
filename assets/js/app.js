/**
 * Sonatel IA Hub — Scripts frontend
 */

'use strict';

/* ── Recherche / filtrage des cards ─────────────────────────── */
function filterCards() {
  const query = document.getElementById('searchInput').value.toLowerCase().trim();
  const cards = document.querySelectorAll('#cardsGrid .card');
  let visible = 0;

  cards.forEach(card => {
    const name  = (card.dataset.name  || '').toLowerCase();
    const title = card.querySelector('h3').textContent.toLowerCase();
    const desc  = card.querySelector('p').textContent.toLowerCase();
    const match = !query || name.includes(query) || title.includes(query) || desc.includes(query);

    card.style.display = match ? '' : 'none';
    if (match) visible++;
  });

  const countEl = document.getElementById('cardCount');
  if (countEl) {
    countEl.textContent = visible + ' App' + (visible > 1 ? 's' : '');
  }
}

/* ── Initialisation ──────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('searchInput');
  if (input) {
    // Filtrage en temps réel
    input.addEventListener('input', filterCards);
    // Validation sur Entrée
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') filterCards();
    });
  }
});
