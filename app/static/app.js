/* ============================================================
   BookBuddy — Frontend Application
   ============================================================ */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let allChildren = [];
let allTags = [];
let currentView = 'library';

const submit = {
  step: 1,
  imageFile: null,
  identified: null,   // {title, author, confidence, above_threshold}
  metadata: null,     // enriched metadata from API
  isDuplicate: false,
  existingBookId: null,
  forceDuplicate: false,
  selections: {},     // childId (string) -> rating string
};

let libSearchTimer = null;
let libStatus = 'library';
let lastRecs = [];
let currentModalBook = null;

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

async function apiFetch(method, path, body) {
  const opts = { method, headers: {} };
  if (body) {
    if (body instanceof FormData) {
      opts.body = body;
    } else {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = 'Request failed';
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}
const GET  = path        => apiFetch('GET',    path);
const POST = (path, b)   => apiFetch('POST',   path, b);
const PUT  = (path, b)   => apiFetch('PUT',    path, b);
const DEL  = path        => apiFetch('DELETE', path);

function showLoading(on, msg = 'Loading…') {
  const el = document.getElementById('loading-overlay');
  el.classList.toggle('hidden', !on);
  if (on) document.getElementById('loading-message').textContent = msg;
}

let toastTimer = null;
function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast' + (type ? ' ' + type : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 3500);
}

function ratingEmoji(r) {
  return { love: '❤️', like: '👍', neutral: '😐', dislike: '👎', hate: '🚫' }[r] || r;
}
function ratingLabel(r) {
  return { love: 'Love', like: 'Like', neutral: 'Neutral', dislike: 'Dislike', hate: 'Hate' }[r] || r;
}
const READING_LEVEL_LABEL = {
  picture_book: 'Picture Book',
  early_reader: 'Early Reader',
  chapter_book: 'Chapter Book',
  middle_grade: 'Middle Grade',
};
function readingLevelBadge(level) {
  const label = READING_LEVEL_LABEL[level];
  if (!label) return '';
  return `<span class="rl-badge rl-${level.replace(/_/g,'-')}">${label}</span>`;
}
function chipClass(r) {
  return `chip chip-${r}`;
}
function calcAge(birthday) {
  const b = new Date(birthday);
  const today = new Date();
  let age = today.getFullYear() - b.getFullYear();
  if (today.getMonth() < b.getMonth() || (today.getMonth() === b.getMonth() && today.getDate() < b.getDate())) age--;
  return age;
}
function formatDate(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); }
  catch { return iso; }
}
function getTodayDate() {
  return new Date().toISOString().split('T')[0]; // YYYY-MM-DD
}
function esc(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function coverImg(url, cls = '') {
  if (url) return `<img src="${esc(url)}" class="${cls}" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" alt="Cover" />`;
  return '';
}
async function compressImage(file) {
  return new Promise(resolve => {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    const img = new Image();
    const blobUrl = URL.createObjectURL(file);
    img.onload = () => {
      const MAX = 1400;
      let { width, height } = img;
      if (width > MAX) { height = Math.round(height * MAX / width); width = MAX; }
      canvas.width = width; canvas.height = height;
      ctx.drawImage(img, 0, 0, width, height);
      URL.revokeObjectURL(blobUrl);
      canvas.toBlob(resolve, 'image/jpeg', 0.82);
    };
    img.src = blobUrl;
  });
}

async function compressAvatar(file) {
  return new Promise(resolve => {
    const SIZE = 200;
    const canvas = document.createElement('canvas');
    canvas.width = SIZE; canvas.height = SIZE;
    const ctx = canvas.getContext('2d');
    const img = new Image();
    const blobUrl = URL.createObjectURL(file);
    img.onload = () => {
      // Centre-crop to square before resizing
      const min = Math.min(img.width, img.height);
      const sx = (img.width  - min) / 2;
      const sy = (img.height - min) / 2;
      ctx.drawImage(img, sx, sy, min, min, 0, 0, SIZE, SIZE);
      URL.revokeObjectURL(blobUrl);
      resolve(canvas.toDataURL('image/jpeg', 0.82));
    };
    img.src = blobUrl;
  });
}

async function fetchAllTags() {
  try { allTags = await GET('/api/tags'); } catch { allTags = []; }
}

// Tag suggestion helpers — works for any input + suggestions container pair
function showTagSuggestions(inputId, suggestionsId) {
  const input = document.getElementById(inputId);
  const container = document.getElementById(suggestionsId);
  if (!input || !container) return;

  const parts = input.value.split(',');
  const partial = parts[parts.length - 1].trim().toLowerCase();
  const already  = parts.slice(0, -1).map(t => t.trim().toLowerCase()).filter(Boolean);

  const matches = allTags
    .filter(t => t.includes(partial) && !already.includes(t))
    .slice(0, 10);

  if (!matches.length || (!partial && already.length === allTags.length)) {
    container.innerHTML = '';
    return;
  }

  container.innerHTML = matches.map((t, i) =>
    `<button type="button" class="tag-suggestion"
      onclick="applyTagSuggestion('${inputId}','${suggestionsId}',${i})">${esc(t)}</button>`
  ).join('');

  // stash matches on the element so onclick can retrieve without embedding strings
  container._matches = matches;
}

function applyTagSuggestion(inputId, suggestionsId, index) {
  const container = document.getElementById(suggestionsId);
  const tag = container._matches?.[index];
  if (!tag) return;

  const input = document.getElementById(inputId);
  const parts = input.value.split(',').map(t => t.trim()).filter(Boolean);
  parts.pop(); // remove the partial being typed
  parts.push(tag);
  input.value = parts.join(', ') + ', ';
  input.focus();
  showTagSuggestions(inputId, suggestionsId); // refresh remaining suggestions
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

function setHeader(title, showBack = false, actions = '') {
  document.getElementById('header-title').textContent = title;
  document.getElementById('header-back-btn').classList.toggle('hidden', !showBack);
  document.getElementById('header-actions').innerHTML = actions;
}

function handleBack() {
  if (currentView === 'profiles') showView('settings');
  else showView('library');
}

async function showView(name) {
  currentView = name;

  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  const el = document.getElementById('view-' + name);
  if (el) el.classList.remove('hidden');

  // Update nav
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const navEl = document.querySelector(`.nav-item[data-view="${name}"]`);
  if (navEl) navEl.classList.add('active');

  // Per-view init
  switch (name) {
    case 'library':
      setHeader('📚 BookBuddy', false);
      await loadLibrary();
      break;
    case 'submit':
      setHeader('Add a Book', false);
      resetSubmit();
      break;
    case 'recommendations':
      setHeader('⭐ Recommendations', false);
      await initRecommendations();
      break;
    case 'settings':
      setHeader('⚙️ Settings', false);
      await loadSettings();
      break;
    case 'profiles':
      setHeader('Children', true);
      await loadProfiles();
      break;
  }
}

// ---------------------------------------------------------------------------
// Library
// ---------------------------------------------------------------------------

function switchLibStatus(status) {
  libStatus = status;
  document.querySelectorAll('.lib-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('lib-tab-' + status)?.classList.add('active');
  // Rating filter only makes sense for library items
  const ratingFilter = document.getElementById('lib-rating-filter');
  if (ratingFilter) ratingFilter.closest('.filter-row').style.display = status === 'wishlist' ? 'none' : '';
  loadLibrary();
}

async function loadLibrary() {
  const search   = document.getElementById('lib-search')?.value.trim() ?? '';
  const childId  = document.getElementById('lib-child-filter')?.value ?? '';
  const rating   = document.getElementById('lib-rating-filter')?.value ?? '';
  const sort     = document.getElementById('lib-sort')?.value ?? 'date_desc';
  const level    = document.getElementById('lib-level-filter')?.value ?? '';

  const params = new URLSearchParams();
  if (search)  params.set('search', search);
  if (childId) params.set('child_id', childId);
  if (rating)  params.set('rating', rating);
  if (sort)    params.set('sort', sort);
  if (level)   params.set('reading_level', level);
  params.set('status', libStatus);

  try {
    const books = await GET('/api/books?' + params.toString());
    renderLibrary(books);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function debounceLibraryLoad() {
  clearTimeout(libSearchTimer);
  libSearchTimer = setTimeout(loadLibrary, 300);
}

function renderLibrary(books) {
  const list = document.getElementById('lib-book-list');
  if (!books.length) {
    const isWishlist = libStatus === 'wishlist';
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">${isWishlist ? '📋' : '📚'}</div>
        <h3>${isWishlist ? 'Wishlist is empty' : 'Your library is empty'}</h3>
        <p>${isWishlist
          ? 'Find a book you want to read and tap <strong>Save to Wishlist</strong> in step 3 of adding a book, or save from the Recommendations screen.'
          : 'Add your first book by tapping the <strong>+</strong> button below.'}</p>
      </div>`;
    return;
  }
  list.innerHTML = books.map(renderBookCard).join('');
}

function renderBookCard(book) {
  const ratings = Object.values(book.ratings || {});
  const chips = ratings.map(r =>
    `<span class="${chipClass(r.rating)}">${esc(r.child_name)} ${ratingEmoji(r.rating)}</span>`
  ).join('');

  const coverHtml = book.cover_url
    ? `<img src="${esc(book.cover_url)}" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" alt="" /><div class="book-cover-placeholder" style="display:none">📖</div>`
    : `<div class="book-cover-placeholder">📖</div>`;

  const isWishlist = book.status === 'wishlist';

  return `
    <div class="book-card${isWishlist ? ' book-card-wishlist' : ''}" onclick="openBookDetail(${book.id})">
      <div class="book-cover-wrap">${coverHtml}</div>
      <div class="book-card-info">
        <div class="book-card-title">${esc(book.title)}</div>
        <div class="book-card-author">${esc(book.author)}</div>
        ${book.series ? `<div class="book-card-series">📚 ${esc(book.series)}</div>` : ''}
        ${book.reading_level ? `<div class="book-card-meta">${readingLevelBadge(book.reading_level)}</div>` : ''}
        ${isWishlist
          ? `<div class="book-card-wishlist-badge">📋 Want to read</div>`
          : chips ? `<div class="rating-chips">${chips}</div>` : ''}
        <div class="book-card-date">${formatDate(book.created_at)}</div>
      </div>
    </div>`;
}

async function openBookDetail(bookId) {
  try {
    const book = await GET('/api/books/' + bookId);
    currentModalBook = book;
    renderBookDetail(book);
    document.getElementById('modal-overlay').classList.remove('hidden');
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function renderBookDetail(book) {
  const ratings = Object.values(book.ratings || {});

  const coverHtml = book.cover_url
    ? `<img src="${esc(book.cover_url)}" class="detail-cover" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" alt="Cover" /><div class="detail-cover-placeholder" style="display:none">📖</div>`
    : `<div class="detail-cover-placeholder">📖</div>`;

  const metaItems = [
    book.published_date ? `<div class="detail-meta-item"><strong>Published</strong> ${esc(book.published_date)}</div>` : '',
    book.page_count     ? `<div class="detail-meta-item"><strong>Pages</strong> ${book.page_count}</div>` : '',
    book.isbn           ? `<div class="detail-meta-item"><strong>ISBN</strong> ${esc(book.isbn)}</div>` : '',
  ].filter(Boolean).join('');

  const today = getTodayDate();
  const isWishlist = book.status === 'wishlist';

  const ratingsHtml = isWishlist
    ? `<div class="wishlist-rate-prompt">
        <p style="color:var(--text-muted);font-size:14px;margin-bottom:12px">Not rated yet — this book is on your wishlist.</p>
        <button class="btn btn-primary" onclick="rateWishlistBook()">Rate This Book →</button>
      </div>`
    : ratings.length > 0
      ? ratings.map(r => `
        <div class="detail-rating-row">
          <div class="detail-rating-top">
            <span class="detail-rating-name">${esc(r.child_name)}</span>
            <div class="detail-rating-btns">
              ${['love','like','neutral','dislike','hate'].map(rv => `
                <button class="detail-rating-btn ${r.rating===rv?'active':''}" data-rating="${rv}"
                  title="${ratingLabel(rv)}"
                  onclick="changeRating(${r.rating_id}, '${rv}', this, ${book.id})">${ratingEmoji(rv)}</button>
              `).join('')}
            </div>
          </div>
          <div class="detail-rating-date-row">
            <label>Read:</label>
            <input type="date" value="${r.date_read || ''}" max="${today}"
              onchange="updateRatingDate(${r.rating_id}, this.value)" />
          </div>
          <textarea class="detail-rating-notes" placeholder="Notes (optional)…" rows="2"
            onblur="updateRatingNotes(${r.rating_id}, this.value)">${esc(r.notes || '')}</textarea>
        </div>`).join('')
      : '<p style="color:var(--text-muted);font-size:14px">No ratings yet.</p>';

  const genresHtml = (book.genres || []).length
    ? `<div style="font-size:13px;color:var(--text-secondary)">${(book.genres).map(g => `<span class="chip chip-neutral" style="margin:2px">${esc(g)}</span>`).join(' ')}</div>`
    : '';

  document.getElementById('modal-content').innerHTML = `
    ${coverHtml}
    <div class="detail-body">
      <div class="detail-title">${esc(book.title)}</div>
      <div class="detail-author">by ${esc(book.author)}</div>
      ${book.description ? `<div class="detail-desc">${esc(book.description)}</div>` : ''}
      ${metaItems ? `<div class="detail-meta">${metaItems}</div>` : ''}
      ${genresHtml}

      <div class="detail-section" style="margin-top:16px">
        <h4>Ratings</h4>
        <div class="detail-ratings-list">${ratingsHtml}</div>
      </div>

      <div class="detail-section">
        <h4>Tags</h4>
        <div class="detail-tags-input">
          <input type="text" id="detail-tags-input" value="${esc(book.tags || '')}" placeholder="funny, dog, adventure"
            autocomplete="off"
            oninput="showTagSuggestions('detail-tags-input','detail-tags-sugg')"
            onfocus="showTagSuggestions('detail-tags-input','detail-tags-sugg')" />
          <button class="btn btn-outline btn-sm" onclick="saveDetailTags(${book.id})">Save</button>
        </div>
        <div id="detail-tags-sugg" class="tag-suggestions" style="margin-top:8px"></div>
      </div>

      <div class="detail-section">
        <h4>Edit</h4>
        <div class="form-group">
          <label>Title</label>
          <input type="text" id="detail-title-input" value="${esc(book.title)}" />
        </div>
        <div class="form-group" style="margin-top:10px">
          <label>Author</label>
          <input type="text" id="detail-author-input" value="${esc(book.author)}" />
        </div>
        <div class="form-group" style="margin-top:10px">
          <label>Series <span class="label-hint">(optional)</span></label>
          <input type="text" id="detail-series-input" value="${esc(book.series || '')}" placeholder="e.g. Elephant &amp; Piggie" />
        </div>
        <div class="form-group" style="margin-top:10px">
          <label>Reading Level <span class="label-hint">(optional)</span></label>
          <select id="detail-level-select">
            <option value="">Unknown</option>
            <option value="picture_book" ${book.reading_level==='picture_book'?'selected':''}>Picture Book</option>
            <option value="early_reader" ${book.reading_level==='early_reader'?'selected':''}>Early Reader</option>
            <option value="chapter_book" ${book.reading_level==='chapter_book'?'selected':''}>Chapter Book</option>
            <option value="middle_grade" ${book.reading_level==='middle_grade'?'selected':''}>Middle Grade</option>
          </select>
        </div>
        <button class="btn btn-outline btn-sm" style="margin-top:12px" onclick="saveDetailEdits(${book.id})">Save Changes</button>
      </div>

      <div class="detail-actions">
        <button class="btn btn-ghost" onclick="closeModal()">Close</button>
        <button class="btn btn-danger" onclick="confirmDeleteBook(${book.id}, '${esc(book.title).replace(/'/g,"\\'")}')">Delete Book</button>
      </div>
    </div>`;
}

async function changeRating(ratingId, newRating, btn, bookId) {
  try {
    await PUT('/api/ratings/' + ratingId, { rating: newRating });
    btn.closest('.detail-rating-btns').querySelectorAll('.detail-rating-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.rating === newRating);
    });
    loadLibrary();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function updateRatingDate(ratingId, dateVal) {
  try {
    await PUT('/api/ratings/' + ratingId, { date_read: dateVal || null });
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function updateRatingNotes(ratingId, notesText) {
  try {
    await PUT('/api/ratings/' + ratingId, { notes: notesText || null });
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function rateWishlistBook() {
  if (!currentModalBook) return;
  const book = currentModalBook;
  closeModal();
  showView('submit');
  setTimeout(() => {
    submit.metadata = {
      title: book.title, author: book.author,
      isbn: book.isbn, cover_url: book.cover_url,
      description: book.description, published_date: book.published_date,
      page_count: book.page_count, genres: book.genres,
      google_books_id: book.google_books_id,
    };
    submit.isDuplicate = true;
    submit.existingBookId = book.id;
    submit.forceDuplicate = true;
    renderMetadataCard(submit.metadata);
    document.getElementById('duplicate-banner').classList.remove('hidden');
    document.getElementById('duplicate-banner').innerHTML =
      '<strong>Moving from Wishlist.</strong> Rate it to add to your library.';
    showSubmitStep(3);
  }, 50);
}

async function saveDetailTags(bookId) {
  const tags = document.getElementById('detail-tags-input').value.trim();
  try {
    await PUT('/api/books/' + bookId, { tags });
    showToast('Tags saved', 'success');
    loadLibrary();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function saveDetailEdits(bookId) {
  const title         = document.getElementById('detail-title-input').value.trim();
  const author        = document.getElementById('detail-author-input').value.trim();
  const series        = document.getElementById('detail-series-input')?.value.trim() ?? null;
  const reading_level = document.getElementById('detail-level-select')?.value ?? null;
  if (!title || !author) { showToast('Title and author are required', 'error'); return; }
  try {
    await PUT('/api/books/' + bookId, { title, author, series: series || null, reading_level: reading_level || null });
    showToast('Book updated', 'success');
    const book = await GET('/api/books/' + bookId);
    currentModalBook = book;
    renderBookDetail(book);
    loadLibrary();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function confirmDeleteBook(bookId, title) {
  if (confirm(`Delete "${title}"?\n\nThis will also remove all ratings for this book.`)) {
    deleteBook(bookId);
  }
}

async function deleteBook(bookId) {
  try {
    await DEL('/api/books/' + bookId);
    closeModal();
    showToast('Book deleted', 'success');
    loadLibrary();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function exportCsv() {
  window.location.href = '/api/export/csv';
}

// ---------------------------------------------------------------------------
// Submit Flow
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Submit mode switching (photo ↔ search)
// ---------------------------------------------------------------------------

function switchSubmitMode(mode) {
  document.getElementById('submit-mode-photo').classList.toggle('hidden', mode !== 'photo');
  document.getElementById('submit-mode-search').classList.toggle('hidden', mode !== 'search');
  document.getElementById('mode-tab-photo').classList.toggle('active', mode === 'photo');
  document.getElementById('mode-tab-search').classList.toggle('active', mode === 'search');
  if (mode === 'search') {
    setTimeout(() => document.getElementById('title-search-input')?.focus(), 80);
  }
}

async function runTitleSearch() {
  const title  = document.getElementById('title-search-input').value.trim();
  const author = document.getElementById('author-search-input').value.trim();
  if (!title) { showToast('Enter a title to search', 'error'); return; }

  showLoading(true, 'Searching…');
  try {
    const q = author ? `${title} ${author}` : title;
    lastSearchResults = await GET('/api/books/search?q=' + encodeURIComponent(q));
    const container = document.getElementById('title-search-results');
    if (!lastSearchResults.length) {
      container.innerHTML = '<p style="color:var(--text-muted);font-size:14px;text-align:center;padding:16px 0">No results found. Try a different title or spelling.</p>';
      return;
    }
    container.innerHTML = lastSearchResults.map((item, i) => `
      <div class="search-result-item" onclick="pickSearchResult(${i})">
        ${item.cover_url
          ? `<img class="search-result-thumb" src="${esc(item.cover_url)}" loading="lazy" onerror="this.src=''" alt="" />`
          : `<div class="search-result-thumb" style="display:flex;align-items:center;justify-content:center;font-size:20px">📖</div>`}
        <div class="search-result-info">
          <div class="title">${esc(item.title)}</div>
          <div class="author">${esc(item.author)}</div>
        </div>
      </div>`).join('');
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    showLoading(false);
  }
}

// ---------------------------------------------------------------------------
// Bulk import
// ---------------------------------------------------------------------------

function showBulkImportModal() {
  const childCols = allChildren.map(c => c.name).join(',');
  const exampleRatings = allChildren.map(() => ',love').join('');

  document.getElementById('modal-content').innerHTML = `
    <div>
      <h3 style="font-size:20px;font-weight:800;margin-bottom:12px">Bulk Import</h3>
      <div class="alert alert-info" style="margin-bottom:16px">
        Upload a CSV to add multiple books at once. Cover art and metadata are fetched automatically.
      </div>

      <details style="margin-bottom:16px">
        <summary style="cursor:pointer;font-weight:600;font-size:14px;color:var(--primary);padding:4px 0">
          📋 CSV format (tap to expand)
        </summary>
        <div style="margin-top:12px;font-size:13px;color:var(--text-secondary);line-height:1.7">
          <p style="margin-bottom:6px"><strong>Required header row:</strong></p>
          <code style="display:block;background:var(--surface-raised);padding:10px 12px;border-radius:var(--r-md);font-size:12px;overflow-x:auto;white-space:pre;margin-bottom:10px">Title,Author,Series,Tags,${childCols || 'ChildName'}</code>
          <ul style="margin-left:16px;display:flex;flex-direction:column;gap:3px">
            <li><strong>Title</strong> — required</li>
            <li><strong>Author</strong> — optional but recommended</li>
            <li><strong>Series</strong> — optional (e.g. Dragon Masters)</li>
            <li><strong>Tags</strong> — optional; wrap in quotes if they contain commas</li>
            <li><strong>Child columns</strong> — use: love · like · neutral · dislike · hate · (blank)</li>
          </ul>
          ${allChildren.length ? `
          <p style="margin-top:10px"><strong>Your child column headers:</strong></p>
          <code style="display:block;background:var(--surface-raised);padding:8px 12px;border-radius:var(--r-md);font-size:12px;margin-top:4px">${esc(childCols)}</code>
          <p style="margin-top:10px"><strong>Example row:</strong></p>
          <code style="display:block;background:var(--surface-raised);padding:8px 12px;border-radius:var(--r-md);font-size:12px;overflow-x:auto;white-space:pre;margin-top:4px">The Very Hungry Caterpillar,Eric Carle,,animals${exampleRatings}</code>
          ` : '<p style="margin-top:10px;color:var(--warning)">⚠️ Add children first so ratings can be imported.</p>'}
        </div>
      </details>

      <div class="form-group">
        <label for="bulk-file-input">Choose CSV file</label>
        <input type="file" id="bulk-file-input" accept=".csv,text/csv"
          style="padding:10px;border:1.5px solid var(--border);border-radius:var(--r-md);width:100%" />
      </div>

      <div style="display:flex;gap:10px;margin-top:16px">
        <button class="btn btn-ghost" style="flex:1" onclick="closeModal()">Cancel</button>
        <button class="btn btn-primary" style="flex:1" onclick="runBulkImport()">Import</button>
      </div>
      <div id="bulk-results" style="margin-top:16px"></div>
    </div>`;
  document.getElementById('modal-overlay').classList.remove('hidden');
}

async function runBulkImport() {
  const fileInput = document.getElementById('bulk-file-input');
  if (!fileInput?.files.length) { showToast('Choose a CSV file first', 'error'); return; }

  showLoading(true, 'Importing… fetching metadata for each book');
  try {
    const fd = new FormData();
    fd.append('file', fileInput.files[0]);
    const result = await POST('/api/books/bulk', fd);

    const noMetaNote = result.no_metadata
      ? ` (${result.no_metadata} saved without cover art)`
      : '';

    document.getElementById('bulk-results').innerHTML = `
      <div class="alert alert-success">
        <strong>Done!</strong> ${result.imported} imported · ${result.duplicates} duplicates skipped${noMetaNote}
      </div>
      ${result.errors.length ? `
        <div style="font-size:13px;color:var(--danger);margin-top:8px">
          <strong>Errors (${result.errors.length}):</strong>
          ${result.errors.slice(0, 6).map(e => `<div style="margin-top:4px">• ${esc(e)}</div>`).join('')}
          ${result.errors.length > 6 ? `<div>…and ${result.errors.length - 6} more</div>` : ''}
        </div>` : ''}`;

    fetchAllTags();
    loadLibrary();
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    showLoading(false);
  }
}

function resetSubmit() {
  submit.step = 1;
  submit.imageFile = null;
  submit.identified = null;
  submit.metadata = null;
  submit.isDuplicate = false;
  submit.existingBookId = null;
  submit.forceDuplicate = false;
  submit.selections = {};

  document.querySelectorAll('.submit-step').forEach(el => el.classList.add('hidden'));
  document.getElementById('submit-step-1').classList.remove('hidden');

  // Reset to photo mode
  switchSubmitMode('photo');

  document.getElementById('image-preview').classList.add('hidden');
  document.getElementById('drop-zone-placeholder').style.display = '';
  document.getElementById('identify-btn').disabled = true;
  document.getElementById('file-input').value = '';
  document.getElementById('camera-input').value = '';

  const titleInput = document.getElementById('title-search-input');
  if (titleInput) titleInput.value = '';
  const authorInput = document.getElementById('author-search-input');
  if (authorInput) authorInput.value = '';
  const resultsDiv = document.getElementById('title-search-results');
  if (resultsDiv) resultsDiv.innerHTML = '';

  const seriesInput = document.getElementById('book-series');
  if (seriesInput) seriesInput.value = '';
  document.getElementById('book-tags').value = '';
  libStatus = libStatus; // preserve current tab

  const panel = document.getElementById('fix-search-panel');
  if (panel) panel.classList.add('hidden');
  const manual = document.getElementById('manual-entry-panel');
  if (manual) manual.classList.add('hidden');
}

function showSubmitStep(n) {
  document.querySelectorAll('.submit-step').forEach(el => el.classList.add('hidden'));
  const target = document.getElementById('submit-step-' + n);
  if (target) target.classList.remove('hidden');
  // Scroll to top of main content
  document.getElementById('main-content').scrollTop = 0;
}

function triggerFileInput(useCamera) {
  if (useCamera) {
    document.getElementById('camera-input').click();
  } else {
    document.getElementById('file-input').click();
  }
}

function handleFileChange(event) {
  const file = event.target.files[0];
  if (!file) return;
  submit.imageFile = file;

  const preview = document.getElementById('image-preview');
  const placeholder = document.getElementById('drop-zone-placeholder');
  const reader = new FileReader();
  reader.onload = e => {
    preview.src = e.target.result;
    preview.classList.remove('hidden');
    placeholder.style.display = 'none';
  };
  reader.readAsDataURL(file);
  document.getElementById('identify-btn').disabled = false;
}

async function runIdentify() {
  if (!submit.imageFile) return;
  showLoading(true, 'Identifying book…');
  try {
    const compressed = await compressImage(submit.imageFile);
    const fd = new FormData();
    fd.append('image', compressed, 'cover.jpg');
    const result = await POST('/api/identify', fd);
    submit.identified = result;
    showIdentifyResult(result);
    showSubmitStep(2);
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    showLoading(false);
  }
}

function confirmIdentification() {
  if (!submit.identified) return;
  fetchMetadata(submit.identified.title || '', submit.identified.author || '', '');
}

function showIdentifyResult(result) {
  const area = document.getElementById('identify-result-area');
  const pct = Math.round((result.confidence || 0) * 100);
  const barColor = result.above_threshold ? 'var(--success)' : 'var(--accent)';

  const cardHtml = `
    <div class="identify-card">
      <div class="identify-card-title">${esc(result.title || 'Unknown title')}</div>
      <div class="identify-card-author">${esc(result.author || 'Unknown author')}</div>
      <div class="confidence-bar-wrap">
        <div class="confidence-bar-track">
          <div class="confidence-bar-fill" style="width:${pct}%;background:${barColor}"></div>
        </div>
        <div class="confidence-label">${pct}% sure</div>
      </div>
    </div>`;

  if (result.above_threshold) {
    area.innerHTML = cardHtml + `
      <a class="not-right-link" href="#" onclick="event.preventDefault();showFixOptions()">Not quite right? Fix it →</a>
      <div class="step-actions">
        <button class="btn btn-primary btn-lg" onclick="confirmIdentification()">
          Looks right — Continue →
        </button>
      </div>`;
  } else {
    area.innerHTML = `
      <div class="warning-banner">
        ⚠️ Low confidence — we're not sure about this match. Please verify or correct it below.
      </div>
      ${cardHtml}
      <div class="fix-action-row">
        <button class="btn btn-primary" onclick="showFixSearch()">🔍 Fix Match</button>
        <button class="btn btn-outline" onclick="showManualEntry()">✏️ Enter Manually</button>
      </div>`;
  }

  // Reset panels
  document.getElementById('fix-search-panel').classList.add('hidden');
  document.getElementById('manual-entry-panel').classList.add('hidden');
}

function showFixOptions() {
  const area = document.getElementById('identify-result-area');
  const actionDiv = area.querySelector('.fix-action-row') || area.appendChild(document.createElement('div'));
  actionDiv.className = 'fix-action-row';
  actionDiv.style.marginTop = '12px';
  actionDiv.innerHTML = `
    <button class="btn btn-primary" onclick="showFixSearch()">🔍 Fix Match</button>
    <button class="btn btn-outline" onclick="showManualEntry()">✏️ Enter Manually</button>`;
  // hide the "not right" link
  const link = document.querySelector('.not-right-link');
  if (link) link.style.display = 'none';
  // hide the confirm button row
  const stepActions = document.querySelector('#submit-step-2 .step-actions');
  if (stepActions) stepActions.style.display = 'none';
}

function showFixSearch() {
  document.getElementById('fix-search-panel').classList.remove('hidden');
  document.getElementById('manual-entry-panel').classList.add('hidden');
  document.getElementById('fix-search-input').focus();
}

function showManualEntry() {
  document.getElementById('manual-entry-panel').classList.remove('hidden');
  document.getElementById('fix-search-panel').classList.add('hidden');
  document.getElementById('manual-title').focus();
}

// Store search results so onclick handlers can reference by index (avoids embedding strings in HTML)
let lastSearchResults = [];

async function runFixSearch() {
  const q = document.getElementById('fix-search-input').value.trim();
  if (!q) return;
  showLoading(true, 'Searching…');
  try {
    lastSearchResults = await GET('/api/books/search?q=' + encodeURIComponent(q));
    renderSearchResults(lastSearchResults);
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    showLoading(false);
  }
}

function renderSearchResults(items) {
  const container = document.getElementById('fix-search-results');
  if (!items.length) {
    container.innerHTML = '<p style="color:var(--text-muted);font-size:14px;text-align:center;padding:12px 0">No results found.</p>';
    return;
  }
  container.innerHTML = items.map((item, i) => `
    <div class="search-result-item" onclick="pickSearchResult(${i})">
      ${item.cover_url
        ? `<img class="search-result-thumb" src="${esc(item.cover_url)}" loading="lazy" onerror="this.src=''" alt="" />`
        : `<div class="search-result-thumb" style="display:flex;align-items:center;justify-content:center;font-size:20px">📖</div>`}
      <div class="search-result-info">
        <div class="title">${esc(item.title)}</div>
        <div class="author">${esc(item.author)}</div>
      </div>
    </div>`).join('');
}

function pickSearchResult(index) {
  const item = lastSearchResults[index];
  if (!item) return;
  fetchMetadata(item.title || '', item.author || '', item.google_books_id || '');
}

async function runManualSearch() {
  const title  = document.getElementById('manual-title').value.trim();
  const author = document.getElementById('manual-author').value.trim();
  if (!title) { showToast('Please enter a title', 'error'); return; }
  await fetchMetadata(title, author, '');
}

async function fetchMetadata(title, author, googleBooksId) {
  showLoading(true, 'Fetching book details…');
  try {
    const params = new URLSearchParams();
    if (googleBooksId) params.set('google_books_id', googleBooksId);
    else { if (title) params.set('title', title); if (author) params.set('author', author); }

    const [meta, dupCheck] = await Promise.all([
      GET('/api/books/metadata?' + params.toString()),
      GET('/api/books/check-duplicate?' + params.toString()),
    ]);

    submit.metadata = meta;
    submit.isDuplicate = dupCheck.exists;
    submit.existingBookId = dupCheck.book_id;
    submit.forceDuplicate = false;

    renderMetadataCard(meta);
    showSubmitStep(3);
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    showLoading(false);
  }
}

function renderMetadataCard(meta) {
  const dupBanner = document.getElementById('duplicate-banner');
  dupBanner.classList.toggle('hidden', !submit.isDuplicate);

  const coverHtml = meta.cover_url
    ? `<img src="${esc(meta.cover_url)}" class="metadata-cover" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" alt="Cover" /><div class="metadata-cover-placeholder" style="display:none">📖</div>`
    : `<div class="metadata-cover-placeholder">📖</div>`;

  const metaItems = [
    meta.published_date ? `<div class="metadata-meta-item"><strong>Published:</strong> ${esc(meta.published_date)}</div>` : '',
    meta.page_count     ? `<div class="metadata-meta-item"><strong>Pages:</strong> ${meta.page_count}</div>` : '',
  ].filter(Boolean).join('');

  document.getElementById('metadata-card').innerHTML = `
    ${coverHtml}
    <div class="metadata-body">
      <div class="metadata-title">${esc(meta.title)}</div>
      <div class="metadata-author">by ${esc(meta.author)}</div>
      ${meta.description ? `<div class="metadata-desc">${esc(meta.description)}</div>` : ''}
      ${metaItems ? `<div class="metadata-meta">${metaItems}</div>` : ''}
    </div>`;
}

function goBackToStep2() {
  showSubmitStep(2);
}

async function proceedToRating() {
  if (submit.isDuplicate) submit.forceDuplicate = true;
  showLoading(true);
  try {
    // Refresh children list
    allChildren = await GET('/api/children');
  } catch (e) {
    allChildren = [];
  } finally {
    showLoading(false);
  }

  if (!allChildren.length) {
    showToast('Add at least one child in Settings → Children first', 'error');
    return;
  }

  submit.selections = {};
  renderChildRatingList();
  showSubmitStep(4);
}

async function addToWishlist() {
  const meta = submit.metadata;
  if (!meta) return;
  showLoading(true, 'Saving to wishlist…');
  try {
    const result = await POST('/api/books/submit', {
      title: meta.title, author: meta.author,
      isbn: meta.isbn || null, cover_url: meta.cover_url || null,
      description: meta.description || null, published_date: meta.published_date || null,
      page_count: meta.page_count || null, genres: meta.genres || [],
      google_books_id: meta.google_books_id || null,
      ratings: [], status: 'wishlist', force_duplicate: false,
    });
    if (result.duplicate) {
      showToast('Already in your library or wishlist', 'warning');
      return;
    }
    document.getElementById('success-message').textContent =
      `"${meta.title}" saved to your wishlist!`;
    showSubmitStep('success');
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    showLoading(false);
  }
}

function renderChildRatingList() {
  const container = document.getElementById('child-rating-list');
  container.innerHTML = allChildren.map(child => `
    <div class="child-rating-card" id="child-card-${child.id}">
      <div class="child-card-header" onclick="toggleChildSelect(${child.id})">
        <div class="child-avatar" id="child-avatar-${child.id}">
          ${child.avatar
            ? `<img src="${esc(child.avatar)}" alt="${esc(child.name)}" />`
            : esc(child.name.charAt(0).toUpperCase())}
        </div>
        <div style="flex:1">
          <div class="child-card-name">${esc(child.name)}</div>
          <div class="child-card-age">${child.age} years old</div>
        </div>
        <div class="child-check" id="child-check-${child.id}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        </div>
      </div>
      <div class="rating-buttons hidden" id="rating-btns-${child.id}">
        ${['love','like','neutral','dislike','hate'].map(r => `
          <button class="rating-btn" data-rating="${r}" id="rating-${child.id}-${r}"
            onclick="setChildRating(${child.id}, '${r}', this)">
            ${ratingEmoji(r)}<span>${ratingLabel(r)}</span>
          </button>`).join('')}
      </div>
      <div class="rating-date-row hidden" id="rating-date-${child.id}">
        <label for="rating-date-input-${child.id}">Date read <span class="label-hint">(optional)</span></label>
        <input type="date" id="rating-date-input-${child.id}" max="${getTodayDate()}"
          onchange="setChildDateRead(${child.id}, this.value)" />
      </div>
      <div class="rating-notes-row hidden" id="rating-notes-${child.id}">
        <input type="text" id="rating-notes-input-${child.id}"
          placeholder="Notes — loved the dog, too scary, etc. (optional)"
          oninput="setChildNotes(${child.id}, this.value)" />
      </div>
    </div>`).join('');
}

function toggleChildSelect(childId) {
  const isSelected = `${childId}` in submit.selections;
  if (isSelected) {
    delete submit.selections[`${childId}`];
    document.getElementById('child-card-' + childId).classList.remove('selected');
    document.getElementById('child-avatar-' + childId).classList.remove('selected');
    document.getElementById('child-check-' + childId).classList.remove('checked');
    document.getElementById('rating-btns-' + childId).classList.add('hidden');
    document.getElementById('rating-date-' + childId).classList.add('hidden');
    document.getElementById('rating-notes-' + childId).classList.add('hidden');
  } else {
    submit.selections[`${childId}`] = { rating: null, date_read: getTodayDate(), notes: null };
    document.getElementById('child-card-' + childId).classList.add('selected');
    document.getElementById('child-avatar-' + childId).classList.add('selected');
    document.getElementById('child-check-' + childId).classList.add('checked');
    document.getElementById('rating-btns-' + childId).classList.remove('hidden');
    document.getElementById('rating-date-' + childId).classList.remove('hidden');
    document.getElementById('rating-notes-' + childId).classList.remove('hidden');
    const di = document.getElementById('rating-date-input-' + childId);
    if (di && !di.value) di.value = getTodayDate();
  }
  updateSaveButton();
}

function setChildRating(childId, rating, btn) {
  const sel = submit.selections[`${childId}`];
  if (sel) sel.rating = rating;
  else submit.selections[`${childId}`] = { rating, date_read: getTodayDate() };
  btn.closest('.rating-buttons').querySelectorAll('.rating-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.rating === rating);
  });
  updateSaveButton();
}

function setChildDateRead(childId, dateVal) {
  const sel = submit.selections[`${childId}`];
  if (sel) sel.date_read = dateVal || null;
}

function setChildNotes(childId, text) {
  const sel = submit.selections[`${childId}`];
  if (sel) sel.notes = text || null;
}

function updateSaveButton() {
  const selected = Object.entries(submit.selections);
  const allRated = selected.length > 0 && selected.every(([,s]) => s?.rating !== null);
  document.getElementById('save-btn').disabled = !allRated;
}

async function saveSubmission() {
  const meta = submit.metadata;
  if (!meta) return;

  const ratingsList = Object.entries(submit.selections)
    .filter(([,s]) => s?.rating !== null)
    .map(([childId, s]) => ({ child_id: parseInt(childId), rating: s.rating, date_read: s.date_read || null, notes: s.notes || null }));

  if (!ratingsList.length) { showToast('Select at least one child and rating', 'error'); return; }

  const series = document.getElementById('book-series')?.value.trim() || null;
  const tags   = document.getElementById('book-tags').value.trim();

  const payload = {
    title:          meta.title,
    author:         meta.author,
    series:         series,
    isbn:           meta.isbn || null,
    cover_url:      meta.cover_url || null,
    description:    meta.description || null,
    published_date: meta.published_date || null,
    page_count:     meta.page_count || null,
    genres:         meta.genres || [],
    tags:           tags || null,
    google_books_id: meta.google_books_id || null,
    ratings:        ratingsList,
    force_duplicate: submit.forceDuplicate,
  };

  showLoading(true, 'Saving…');
  try {
    const result = await POST('/api/books/submit', payload);
    if (result.duplicate && !payload.force_duplicate) {
      // Shouldn't normally happen (we set force_duplicate), but handle gracefully
      submit.forceDuplicate = true;
      payload.force_duplicate = true;
      await POST('/api/books/submit', payload);
    }
    // Show success and refresh tag cache
    fetchAllTags();
    document.getElementById('success-message').textContent =
      `"${meta.title}" has been added to your library!`;
    showSubmitStep('success');
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    showLoading(false);
  }
}

// ---------------------------------------------------------------------------
// Recommendations
// ---------------------------------------------------------------------------

async function initRecommendations() {
  try {
    allChildren = await GET('/api/children');
  } catch { allChildren = []; }

  const sel = document.getElementById('recs-child-select');
  sel.innerHTML = '<option value="">Select a child…</option>' +
    allChildren.map(c => `<option value="${c.id}">${esc(c.name)} (age ${c.age})</option>`).join('');

  document.getElementById('recs-generate-btn').disabled = true;
  document.getElementById('recs-results').innerHTML =
    allChildren.length === 0
      ? '<div class="empty-state"><div class="empty-icon">👶</div><h3>No children yet</h3><p>Add children in Settings → Children first.</p></div>'
      : '<div class="empty-state" style="padding:32px 0"><div class="empty-icon">⭐</div><p>Select a child and tap Get Recommendations.</p></div>';
}

function onRecsChildChange() {
  document.getElementById('recs-generate-btn').disabled =
    !document.getElementById('recs-child-select').value;
}

async function generateRecommendations() {
  const childId = document.getElementById('recs-child-select').value;
  if (!childId) return;

  showLoading(true, 'Asking Claude for recommendations…');
  try {
    const recs = await GET('/api/recommendations/' + childId);
    lastRecs = recs;
    renderRecommendations(recs);
  } catch (e) {
    showToast(e.message, 'error');
    document.getElementById('recs-results').innerHTML =
      `<div class="alert alert-danger">${esc(e.message)}</div>`;
  } finally {
    showLoading(false);
  }
}

function renderRecommendations(recs) {
  if (!recs.length) {
    document.getElementById('recs-results').innerHTML =
      '<div class="empty-state"><p>No recommendations returned. Try again.</p></div>';
    return;
  }
  const cards = recs.map((rec, i) => {
    const coverHtml = rec.cover_url
      ? `<img src="${esc(rec.cover_url)}" loading="lazy" onerror="this.style.display='none'" alt="" />`
      : `<div style="font-size:28px">📖</div>`;
    return `
      <div class="rec-card">
        <div class="rec-cover-wrap">${coverHtml}</div>
        <div class="rec-info">
          <div class="rec-title">${esc(rec.title)}</div>
          <div class="rec-author">by ${esc(rec.author)}</div>
          <div class="rec-reason">${esc(rec.reason)}</div>
          <button class="btn btn-outline btn-sm rec-wishlist-btn" onclick="wishlistRec(${i})">📋 Wishlist</button>
        </div>
      </div>`;
  }).join('');

  document.getElementById('recs-results').innerHTML =
    `<div class="recs-list">${cards}</div>
     <div id="recs-regenerate-row">
       <button class="btn btn-outline" onclick="generateRecommendations()">↺ Regenerate</button>
     </div>`;
}

async function wishlistRec(index) {
  const rec = lastRecs[index];
  if (!rec) return;
  showLoading(true, 'Saving to wishlist…');
  try {
    const result = await POST('/api/books/submit', {
      title: rec.title, author: rec.author,
      cover_url: rec.cover_url || null,
      google_books_id: rec.google_books_id || null,
      genres: [], ratings: [], status: 'wishlist', force_duplicate: false,
    });
    if (result.duplicate) {
      showToast('Already in your library or wishlist', 'warning');
    } else {
      showToast(`"${rec.title}" added to wishlist!`, 'success');
    }
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    showLoading(false);
  }
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

async function loadSettings() {
  try {
    const settings = await GET('/api/settings');
    const threshold = parseFloat(settings.confidence_threshold ?? 0.75);
    const slider = document.getElementById('confidence-slider');
    slider.value = threshold;
    document.getElementById('confidence-value').textContent = threshold.toFixed(2);
  } catch (e) {
    showToast('Failed to load settings', 'error');
  }
}

function updateConfidenceDisplay(value) {
  document.getElementById('confidence-value').textContent = parseFloat(value).toFixed(2);
}

async function saveConfidenceThreshold(value) {
  try {
    await PUT('/api/settings', { key: 'confidence_threshold', value: String(value) });
    showToast('Threshold saved', 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
}

// ---------------------------------------------------------------------------
// Profiles
// ---------------------------------------------------------------------------

async function loadProfiles() {
  try {
    allChildren = await GET('/api/children');
    renderProfiles(allChildren);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function renderProfiles(children) {
  const list = document.getElementById('profiles-list');
  if (!children.length) {
    list.innerHTML = `
      <div class="empty-state" style="padding:32px 0">
        <div class="empty-icon">👶</div>
        <h3>No children yet</h3>
        <p>Add a child to start tracking their reading.</p>
      </div>`;
    return;
  }
  list.innerHTML = children.map(c => `
    <div class="profile-card">
      <div class="profile-avatar">
        ${c.avatar
          ? `<img src="${esc(c.avatar)}" alt="${esc(c.name)}" />`
          : esc(c.name.charAt(0).toUpperCase())}
      </div>
      <div class="profile-info">
        <div class="profile-name">${esc(c.name)}</div>
        <div class="profile-age">Age ${c.age} · Birthday ${formatDate(c.birthday)}</div>
      </div>
      <div class="profile-actions">
        <button class="btn btn-outline btn-sm" onclick="showEditChildForm(${c.id})">Edit</button>
        <button class="btn btn-danger btn-sm" onclick="confirmDeleteChild(${c.id}, '${esc(c.name).replace(/'/g,"\\'")}')">Delete</button>
      </div>
    </div>`).join('');
}

function _childFormHtml({ title, name = '', birthday = '', avatar = null, saveCall }) {
  const today = new Date().toISOString().split('T')[0];
  const initials = name ? esc(name.charAt(0).toUpperCase()) : '👶';
  const avatarContent = avatar
    ? `<img src="${esc(avatar)}" id="cf-avatar-preview-img" alt="avatar" />`
    : `<span id="cf-avatar-preview-img">${initials}</span>`;

  return `
    <div class="child-form">
      <h3>${title}</h3>
      <div class="avatar-upload-wrap">
        <div class="avatar-upload-circle" onclick="document.getElementById('cf-avatar-input').click()">
          ${avatarContent}
        </div>
        <span class="avatar-upload-label">Tap to add photo</span>
        <input type="file" id="cf-avatar-input" accept="image/*" class="hidden"
          onchange="handleAvatarChange(event)" />
      </div>
      <div class="form-group">
        <label for="cf-name">Name</label>
        <input type="text" id="cf-name" value="${esc(name)}" placeholder="e.g. Aidan"
          oninput="updateAvatarInitial()" />
      </div>
      <div class="form-group">
        <label for="cf-birthday">Birthday</label>
        <input type="date" id="cf-birthday" value="${esc(birthday)}" max="${today}" />
      </div>
      <div class="child-form-actions">
        <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
        <button class="btn btn-primary" onclick="${saveCall}">Save</button>
      </div>
    </div>`;
}

// Tracks the avatar data URI currently staged in the form
let pendingAvatar = null;

async function handleAvatarChange(event) {
  const file = event.target.files[0];
  if (!file) return;
  const dataUrl = await compressAvatar(file);
  pendingAvatar = dataUrl;
  const preview = document.getElementById('cf-avatar-preview-img');
  if (preview.tagName === 'IMG') {
    preview.src = dataUrl;
  } else {
    // Replace the span with an img
    const img = document.createElement('img');
    img.id = 'cf-avatar-preview-img';
    img.src = dataUrl;
    img.alt = 'avatar';
    preview.replaceWith(img);
  }
}

function updateAvatarInitial() {
  if (pendingAvatar) return; // already has a real photo
  const name = document.getElementById('cf-name')?.value.trim() ?? '';
  const preview = document.getElementById('cf-avatar-preview-img');
  if (preview && preview.tagName === 'SPAN') {
    preview.textContent = name ? name.charAt(0).toUpperCase() : '👶';
  }
}

function showAddChildForm() {
  pendingAvatar = null;
  document.getElementById('modal-content').innerHTML =
    _childFormHtml({ title: 'Add Child', saveCall: 'saveChild(null)' });
  document.getElementById('modal-overlay').classList.remove('hidden');
  document.getElementById('cf-name').focus();
}

function showEditChildForm(childId) {
  const child = allChildren.find(c => c.id === childId);
  if (!child) return;
  pendingAvatar = null;
  document.getElementById('modal-content').innerHTML =
    _childFormHtml({
      title: `Edit ${esc(child.name)}`,
      name: child.name,
      birthday: child.birthday,
      avatar: child.avatar,
      saveCall: `saveChild(${childId})`,
    });
  document.getElementById('modal-overlay').classList.remove('hidden');
}

async function saveChild(childId) {
  const name     = document.getElementById('cf-name').value.trim();
  const birthday = document.getElementById('cf-birthday').value;
  if (!name)     { showToast('Name is required', 'error'); return; }
  if (!birthday) { showToast('Birthday is required', 'error'); return; }

  // Use the newly uploaded avatar; for edits, null means "don't change"
  const avatar = pendingAvatar;

  try {
    if (childId) {
      await PUT('/api/children/' + childId, { name, birthday, avatar });
      showToast('Child updated', 'success');
    } else {
      await POST('/api/children', { name, birthday, avatar });
      showToast('Child added', 'success');
    }
    closeModal();
    await loadProfiles();
    allChildren = await GET('/api/children');
    populateChildFilters();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function confirmDeleteChild(childId, name) {
  if (confirm(`Deleting ${name} will remove all their ratings. Are you sure?`)) {
    deleteChild(childId);
  }
}

async function deleteChild(childId) {
  try {
    await DEL('/api/children/' + childId);
    showToast('Child deleted', 'success');
    await loadProfiles();
    allChildren = await GET('/api/children');
    populateChildFilters();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

// ---------------------------------------------------------------------------
// Modal
// ---------------------------------------------------------------------------

function closeModal() {
  document.getElementById('modal-overlay').classList.add('hidden');
  document.getElementById('modal-content').innerHTML = '';
}

function closeModalOnOverlay(event) {
  if (event.target === document.getElementById('modal-overlay')) closeModal();
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

function populateChildFilters() {
  const sel = document.getElementById('lib-child-filter');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">All children</option>' +
    allChildren.map(c => `<option value="${c.id}" ${c.id == current ? 'selected' : ''}>${esc(c.name)}</option>`).join('');
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  try {
    [allChildren] = await Promise.all([
      GET('/api/children'),
      fetchAllTags(),
    ]);
    populateChildFilters();
  } catch { allChildren = []; }

  showView('library');
}

document.addEventListener('DOMContentLoaded', init);
