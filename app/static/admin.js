/* =========================================================
   BookBuddy Admin — admin.js
   ========================================================= */

"use strict";

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); msg = j.detail || msg; } catch {}
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

function showToast(msg, type = "info") {
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.getElementById("toast-container").appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function levelLabel(val) {
  return { picture_book: "Picture Book", early_reader: "Early Reader",
           chapter_book: "Chapter Book", middle_grade: "Middle Grade" }[val] || val || "—";
}

function ratingEmoji(r) {
  return { love: "❤️", like: "👍", neutral: "😐", dislike: "👎", hate: "😤" }[r] || r;
}

// ---------------------------------------------------------------------------
// Section navigation
// ---------------------------------------------------------------------------

function showSection(name) {
  document.querySelectorAll(".section").forEach(s => s.classList.remove("active"));
  document.querySelectorAll(".nav-link").forEach(a => a.classList.remove("active"));
  const sec = document.getElementById(`section-${name}`);
  if (sec) sec.classList.add("active");
  const link = document.querySelector(`.nav-link[data-section="${name}"]`);
  if (link) link.classList.add("active");

  // Lazy-load section data
  if (name === "library")  loadLibrary();
  if (name === "covers")   loadCovers();
  if (name === "export")   loadExportStats();
}

// ---------------------------------------------------------------------------
// Library section
// ---------------------------------------------------------------------------

let allBooks = [];         // current result set
let selectedIds = new Set();

async function loadLibrary() {
  const search  = document.getElementById("lib-search").value.trim();
  const status  = document.getElementById("lib-status").value;
  const missing = document.getElementById("lib-missing").value;

  const params = new URLSearchParams();
  if (search)  params.set("search", search);
  if (status)  params.set("status", status);
  if (missing) params.set("missing", missing);

  document.getElementById("lib-loading").style.display = "";
  document.getElementById("lib-empty").style.display = "none";
  document.getElementById("lib-table-wrapper").style.display = "none";

  try {
    allBooks = await api("GET", `/api/admin/books?${params}`);
    selectedIds.clear();
    renderLibraryTable(allBooks);
    updateBulkBar();
  } catch (e) {
    showToast(e.message, "error");
    document.getElementById("lib-loading").style.display = "none";
  }
}

function renderLibraryTable(books) {
  document.getElementById("lib-loading").style.display = "none";

  if (!books.length) {
    document.getElementById("lib-empty").style.display = "";
    document.getElementById("lib-table-wrapper").style.display = "none";
    return;
  }

  document.getElementById("lib-empty").style.display = "none";
  document.getElementById("lib-table-wrapper").style.display = "";

  const tbody = document.getElementById("lib-tbody");
  tbody.innerHTML = "";

  for (const book of books) {
    const tr = document.createElement("tr");
    tr.dataset.id = book.id;
    if (selectedIds.has(book.id)) tr.classList.add("row-selected");

    // Ratings summary
    const ratingsHtml = Object.values(book.ratings || {})
      .map(r => `<span class="rating-chip" title="${r.child_name}">${r.child_name[0]} ${ratingEmoji(r.rating)}</span>`)
      .join("");

    tr.innerHTML = `
      <td class="col-check"><input type="checkbox" class="row-check" data-id="${book.id}" ${selectedIds.has(book.id) ? "checked" : ""}/></td>
      <td class="col-cover">${coverCell(book)}</td>
      <td class="col-title cell-edit" data-field="title" data-id="${book.id}">${esc(book.title)}</td>
      <td class="col-author cell-edit" data-field="author" data-id="${book.id}">${esc(book.author)}</td>
      <td class="col-series cell-edit" data-field="series" data-id="${book.id}">${esc(book.series || "")}</td>
      <td class="col-num cell-edit" data-field="series_order" data-id="${book.id}">${esc(book.series_order || "")}</td>
      <td class="col-level">${levelDropdownCell(book)}</td>
      <td class="col-tags cell-edit" data-field="tags" data-id="${book.id}">${esc(book.tags || "")}</td>
      <td class="col-status">${statusDropdownCell(book)}</td>
      <td class="col-ratings">${ratingsHtml || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td class="col-actions"><div class="row-actions">
        <button class="btn-icon" data-action="refetch" data-id="${book.id}" title="Refetch metadata">&#x21BA;</button>
        <button class="btn-icon danger" data-action="delete" data-id="${book.id}" title="Delete">&#x2715;</button>
      </div></td>
    `;
    tbody.appendChild(tr);
  }

  // Wire up events
  wireCellEdit();
  wireRowChecks();
  wireRowActions();
  wireLevelDropdowns();
  wireStatusDropdowns();
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function coverCell(book) {
  if (book.cover_url) {
    return `<img class="cover-thumb" src="${esc(book.cover_url)}" data-action="cover" data-id="${book.id}" alt="cover" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" /><div class="cover-placeholder" data-action="cover" data-id="${book.id}" style="display:none">📖</div>`;
  }
  return `<div class="cover-placeholder" data-action="cover" data-id="${book.id}">📖</div>`;
}

function levelDropdownCell(book) {
  const levels = [
    ["", "— Level —"],
    ["picture_book", "Picture Book"],
    ["early_reader", "Early Reader"],
    ["chapter_book", "Chapter Book"],
    ["middle_grade", "Middle Grade"],
  ];
  const opts = levels.map(([v, l]) =>
    `<option value="${v}" ${book.reading_level === v ? "selected" : ""}>${l}</option>`
  ).join("");
  return `<select class="level-select" data-id="${book.id}">${opts}</select>`;
}

function statusDropdownCell(book) {
  const s = book.status || "library";
  return `<select class="status-select" data-id="${book.id}">
    <option value="library"  ${s === "library"  ? "selected" : ""}>Library</option>
    <option value="wishlist" ${s === "wishlist" ? "selected" : ""}>Wishlist</option>
  </select>`;
}

// ── Inline cell editing ──

function wireCellEdit() {
  document.querySelectorAll(".cell-edit").forEach(td => {
    td.addEventListener("click", startCellEdit);
  });
}

function startCellEdit(e) {
  const td = e.currentTarget;
  if (td.querySelector("input")) return; // already editing
  const field = td.dataset.field;
  const id = parseInt(td.dataset.id, 10);
  const current = td.textContent.trim();

  const input = document.createElement("input");
  input.type = "text";
  input.value = current;
  td.textContent = "";
  td.appendChild(input);
  input.focus();
  input.select();

  async function commit() {
    const newVal = input.value.trim();
    td.textContent = newVal;
    if (newVal === current) return;
    try {
      const updated = await api("PUT", `/api/books/${id}`, { [field]: newVal });
      // update local cache
      const book = allBooks.find(b => b.id === id);
      if (book) book[field] = updated[field];
      showToast("Saved", "success");
    } catch (err) {
      td.textContent = current;
      showToast(err.message, "error");
    }
  }

  input.addEventListener("blur", commit);
  input.addEventListener("keydown", ev => {
    if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
    if (ev.key === "Escape") {
      input.removeEventListener("blur", commit);
      td.textContent = current;
    }
  });
}

// ── Level dropdown ──

function wireLevelDropdowns() {
  document.querySelectorAll(".level-select").forEach(sel => {
    sel.addEventListener("change", async () => {
      const id = parseInt(sel.dataset.id, 10);
      try {
        await api("PUT", `/api/books/${id}`, { reading_level: sel.value });
        const book = allBooks.find(b => b.id === id);
        if (book) book.reading_level = sel.value;
        showToast("Level updated", "success");
      } catch (err) { showToast(err.message, "error"); }
    });
  });
}

// ── Status dropdown ──

function wireStatusDropdowns() {
  document.querySelectorAll(".status-select").forEach(sel => {
    sel.addEventListener("change", async () => {
      const id = parseInt(sel.dataset.id, 10);
      try {
        await api("PUT", `/api/books/${id}`, { status: sel.value });
        const book = allBooks.find(b => b.id === id);
        if (book) book.status = sel.value;
        showToast("Status updated", "success");
      } catch (err) { showToast(err.message, "error"); }
    });
  });
}

// ── Cover cell ──

document.addEventListener("click", async e => {
  const el = e.target.closest("[data-action='cover']");
  if (!el) return;
  const id = parseInt(el.dataset.id, 10);
  const book = allBooks.find(b => b.id === id);
  const current = book ? book.cover_url || "" : "";
  const url = window.prompt("Enter new cover URL:", current);
  if (url === null) return;
  try {
    const updated = await api("PUT", `/api/books/${id}`, { cover_url: url.trim() });
    if (book) book.cover_url = updated.cover_url;
    // Refresh row cover cell
    const td = document.querySelector(`.col-cover [data-id="${id}"]`)?.closest("td")
             || document.querySelector(`td.col-cover [data-id="${id}"]`)?.parentElement;
    if (td) td.innerHTML = coverCell(updated);
    showToast("Cover updated", "success");
  } catch (err) { showToast(err.message, "error"); }
});

// ── Row checkboxes ──

function wireRowChecks() {
  document.querySelectorAll(".row-check").forEach(cb => {
    cb.addEventListener("change", () => {
      const id = parseInt(cb.dataset.id, 10);
      if (cb.checked) selectedIds.add(id);
      else selectedIds.delete(id);
      const tr = cb.closest("tr");
      if (tr) tr.classList.toggle("row-selected", cb.checked);
      updateBulkBar();
    });
  });

  document.getElementById("select-all").addEventListener("change", e => {
    const checked = e.target.checked;
    document.querySelectorAll(".row-check").forEach(cb => {
      cb.checked = checked;
      const id = parseInt(cb.dataset.id, 10);
      if (checked) selectedIds.add(id);
      else selectedIds.delete(id);
      const tr = cb.closest("tr");
      if (tr) tr.classList.toggle("row-selected", checked);
    });
    updateBulkBar();
  });
}

// ── Per-row actions ──

function wireRowActions() {
  document.querySelectorAll("[data-action='refetch']").forEach(btn => {
    btn.addEventListener("click", () => refetchBook(parseInt(btn.dataset.id, 10)));
  });
  document.querySelectorAll("[data-action='delete']").forEach(btn => {
    btn.addEventListener("click", () => deleteBook(parseInt(btn.dataset.id, 10)));
  });
}

async function refetchBook(id) {
  const book = allBooks.find(b => b.id === id);
  if (!book) return;
  try {
    const meta = await api("GET", `/api/books/metadata?title=${encodeURIComponent(book.title)}&author=${encodeURIComponent(book.author)}`);
    if (!meta || !meta.title) { showToast("No metadata found", "info"); return; }
    const payload = {};
    if (meta.cover_url)      payload.cover_url      = meta.cover_url;
    if (meta.description)    payload.description    = meta.description;
    if (meta.isbn)           payload.isbn           = meta.isbn;
    if (meta.published_date) payload.published_date = meta.published_date;
    await api("PUT", `/api/books/${id}`, payload);
    showToast("Metadata updated", "success");
    loadLibrary();
  } catch (err) { showToast(err.message, "error"); }
}

async function deleteBook(id) {
  const book = allBooks.find(b => b.id === id);
  const title = book ? `"${book.title}"` : `book #${id}`;
  if (!confirm(`Delete ${title}? This cannot be undone.`)) return;
  try {
    await api("DELETE", `/api/books/${id}`);
    showToast("Deleted", "success");
    selectedIds.delete(id);
    loadLibrary();
  } catch (err) { showToast(err.message, "error"); }
}

// ---------------------------------------------------------------------------
// Bulk action bar
// ---------------------------------------------------------------------------

function updateBulkBar() {
  const bar = document.getElementById("bulk-bar");
  const n = selectedIds.size;
  document.getElementById("bulk-count").textContent = `${n} book${n !== 1 ? "s" : ""} selected`;
  bar.classList.toggle("visible", n > 0);
}

// Bulk dropdown toggles
document.querySelectorAll(".bulk-trigger").forEach(btn => {
  btn.addEventListener("click", e => {
    e.stopPropagation();
    const menu = btn.nextElementSibling;
    document.querySelectorAll(".bulk-menu").forEach(m => { if (m !== menu) m.classList.remove("open"); });
    menu.classList.toggle("open");
  });
});
document.addEventListener("click", () => {
  document.querySelectorAll(".bulk-menu").forEach(m => m.classList.remove("open"));
});

// Level menu
document.getElementById("bulk-level-menu").querySelectorAll("a").forEach(a => {
  a.addEventListener("click", async e => {
    e.preventDefault();
    const level = a.dataset.val;
    try {
      await api("POST", "/api/admin/bulk-update", { ids: [...selectedIds], reading_level: level });
      showToast(`Level updated for ${selectedIds.size} books`, "success");
      loadLibrary();
    } catch (err) { showToast(err.message, "error"); }
  });
});

// Status menu
document.getElementById("bulk-status-menu").querySelectorAll("a").forEach(a => {
  a.addEventListener("click", async e => {
    e.preventDefault();
    try {
      await api("POST", "/api/admin/bulk-update", { ids: [...selectedIds], status: a.dataset.val });
      showToast(`Status updated for ${selectedIds.size} books`, "success");
      loadLibrary();
    } catch (err) { showToast(err.message, "error"); }
  });
});

// Add tags
document.getElementById("bulk-add-tags").addEventListener("click", async () => {
  const tags = window.prompt("Add tags (comma-separated):");
  if (!tags || !tags.trim()) return;
  try {
    await api("POST", "/api/admin/bulk-update", { ids: [...selectedIds], tags_append: tags.trim() });
    showToast(`Tags added to ${selectedIds.size} books`, "success");
    loadLibrary();
  } catch (err) { showToast(err.message, "error"); }
});

// Refetch metadata for all selected
document.getElementById("bulk-refetch").addEventListener("click", async () => {
  const ids = [...selectedIds];
  if (!ids.length) return;
  showToast(`Refetching metadata for ${ids.length} books…`, "info");
  let done = 0;
  for (const id of ids) {
    try { await refetchBook(id); } catch {}
    done++;
  }
  showToast(`Refetch complete (${done}/${ids.length})`, "success");
  loadLibrary();
});

// Delete selected
document.getElementById("bulk-delete").addEventListener("click", async () => {
  const ids = [...selectedIds];
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} book(s)? This cannot be undone.`)) return;
  let ok = 0, fail = 0;
  for (const id of ids) {
    try { await api("DELETE", `/api/books/${id}`); ok++; }
    catch { fail++; }
  }
  showToast(`Deleted ${ok} book(s)${fail ? `, ${fail} failed` : ""}`, ok ? "success" : "error");
  selectedIds.clear();
  loadLibrary();
});

// Deselect
document.getElementById("bulk-deselect").addEventListener("click", () => {
  selectedIds.clear();
  document.querySelectorAll(".row-check").forEach(cb => { cb.checked = false; });
  document.getElementById("select-all").checked = false;
  document.querySelectorAll(".row-selected").forEach(tr => tr.classList.remove("row-selected"));
  updateBulkBar();
});

// Escape closes any open inline edit by blurring
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    const input = document.querySelector(".cell-edit input");
    if (input) input.blur();
    document.querySelectorAll(".bulk-menu").forEach(m => m.classList.remove("open"));
  }
});

// Library filter controls
document.getElementById("lib-apply").addEventListener("click", loadLibrary);
document.getElementById("lib-search").addEventListener("keydown", e => {
  if (e.key === "Enter") loadLibrary();
});

// ---------------------------------------------------------------------------
// Import section
// ---------------------------------------------------------------------------

let csvFile = null; // keep file in memory

const dropzone = document.getElementById("dropzone");
const csvInput = document.getElementById("csv-file-input");

dropzone.addEventListener("dragover", e => { e.preventDefault(); dropzone.classList.add("drag-over"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag-over"));
dropzone.addEventListener("drop", e => {
  e.preventDefault();
  dropzone.classList.remove("drag-over");
  const f = e.dataTransfer.files[0];
  if (f) handleCsvFile(f);
});

csvInput.addEventListener("change", () => {
  if (csvInput.files[0]) handleCsvFile(csvInput.files[0]);
});

async function handleCsvFile(file) {
  csvFile = file;
  document.getElementById("drop-filename").textContent = file.name;
  document.getElementById("import-preview").style.display = "none";

  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await fetch("/api/admin/csv/preview", { method: "POST", body: fd });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    renderImportPreview(data);
  } catch (err) {
    showToast(err.message, "error");
  }
}

function renderImportPreview(data) {
  document.getElementById("import-summary").innerHTML =
    `<strong>${data.new_count}</strong> new book${data.new_count !== 1 ? "s" : ""}, ` +
    `<strong>${data.duplicate_count}</strong> duplicate${data.duplicate_count !== 1 ? "s" : ""}`;

  // Build header: Title | Author | Series | [child names] | Status
  const childNames = data.child_names || [];
  const thead = document.getElementById("preview-thead");
  thead.innerHTML = `<tr>
    <th>Title</th><th>Author</th><th>Series</th><th>Tags</th>
    ${childNames.map(n => `<th>${esc(n)}</th>`).join("")}
    <th>Status</th>
  </tr>`;

  const tbody = document.getElementById("preview-tbody");
  tbody.innerHTML = "";
  for (const row of data.rows) {
    const tr = document.createElement("tr");
    tr.className = row.status === "new" ? "row-new" : "row-duplicate";
    tr.innerHTML = `
      <td>${esc(row.title)}</td>
      <td>${esc(row.author)}</td>
      <td>${esc(row.series || "")}</td>
      <td>${esc(row.tags || "")}</td>
      ${childNames.map(n => `<td>${esc(row.ratings[n] || "")}</td>`).join("")}
      <td><span class="badge badge-${row.status}">${row.status}</span></td>
    `;
    tbody.appendChild(tr);
  }

  document.getElementById("import-preview").style.display = "";
}

document.getElementById("commit-import").addEventListener("click", async () => {
  if (!csvFile) { showToast("No file selected", "error"); return; }
  const fd = new FormData();
  fd.append("file", csvFile);
  const resultEl = document.getElementById("import-result");
  resultEl.textContent = "Importing…";
  try {
    const res = await fetch("/api/books/bulk", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    resultEl.textContent = `Done: ${data.imported} imported, ${data.duplicates} duplicates, ${data.errors?.length || 0} errors.`;
    showToast(`Imported ${data.imported} book(s)`, "success");
  } catch (err) {
    resultEl.textContent = "";
    showToast(err.message, "error");
  }
});

// Download template CSV
document.getElementById("download-template").addEventListener("click", async () => {
  try {
    const children = await api("GET", "/api/children");
    const childCols = children.map(c => c.name).join(",");
    const header = `Title,Author,Series,Tags${childCols ? "," + childCols : ""}`;
    const example = `"The Very Hungry Caterpillar","Eric Carle","","picture book${childCols ? "," + children.map(() => "love").join(",") : ""}"`;
    const csv = header + "\n" + example + "\n";
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "bookbuddy_template.csv";
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) { showToast(err.message, "error"); }
});

// ---------------------------------------------------------------------------
// Covers section
// ---------------------------------------------------------------------------

async function loadCovers() {
  const missingOnly = document.getElementById("covers-missing-only").checked;
  const grid = document.getElementById("covers-grid");
  const loadingEl = document.getElementById("covers-loading");

  grid.innerHTML = "";
  loadingEl.style.display = "";

  try {
    const params = new URLSearchParams();
    if (missingOnly) params.set("missing", "cover");
    const books = await api("GET", `/api/admin/books?${params}`);
    loadingEl.style.display = "none";

    if (!books.length) {
      grid.innerHTML = `<p style="color:var(--text-muted);padding:20px">No books found.</p>`;
      return;
    }

    for (const book of books) {
      const card = document.createElement("div");
      card.className = "cover-card";
      card.innerHTML = `
        ${book.cover_url
          ? `<img class="cover-card-img" src="${esc(book.cover_url)}" alt="cover" loading="lazy" onerror="this.style.display='none'" />`
          : `<div class="cover-card-placeholder">📖</div>`}
        <div class="cover-card-title">${esc(book.title)}</div>
        <div class="cover-card-author">${esc(book.author)}</div>
        <div class="cover-card-input">
          <input type="url" placeholder="Cover URL" value="${esc(book.cover_url || "")}" data-id="${book.id}" />
          <button class="btn btn-primary btn-sm" data-id="${book.id}">Save</button>
        </div>
      `;
      grid.appendChild(card);
    }

    // Wire save buttons
    grid.querySelectorAll("button[data-id]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = parseInt(btn.dataset.id, 10);
        const input = btn.previousElementSibling;
        const url = input.value.trim();
        try {
          await api("PUT", `/api/books/${id}`, { cover_url: url });
          showToast("Cover saved", "success");
          // Update preview image in card
          const card = btn.closest(".cover-card");
          const existing = card.querySelector("img, .cover-card-placeholder");
          if (url) {
            if (existing && existing.tagName === "IMG") {
              existing.src = url;
            } else {
              const img = document.createElement("img");
              img.className = "cover-card-img";
              img.src = url;
              img.alt = "cover";
              img.loading = "lazy";
              if (existing) existing.replaceWith(img);
            }
          }
        } catch (err) { showToast(err.message, "error"); }
      });
    });
  } catch (err) {
    loadingEl.style.display = "none";
    showToast(err.message, "error");
  }
}

document.getElementById("covers-load").addEventListener("click", loadCovers);
document.getElementById("covers-missing-only").addEventListener("change", loadCovers);

// ---------------------------------------------------------------------------
// Export section
// ---------------------------------------------------------------------------

async function loadExportStats() {
  const el = document.getElementById("export-stats");
  try {
    const [books, children] = await Promise.all([
      api("GET", "/api/admin/books"),
      api("GET", "/api/children"),
    ]);
    const totalRatings = books.reduce((sum, b) => sum + Object.keys(b.ratings || {}).length, 0);
    const libCount = books.filter(b => b.status === "library").length;
    const wishCount = books.filter(b => b.status === "wishlist").length;
    el.innerHTML = `
      <strong>${books.length}</strong> total books
        (${libCount} in library, ${wishCount} on wishlist)<br/>
      <strong>${totalRatings}</strong> total ratings<br/>
      <strong>${children.length}</strong> children: ${children.map(c => c.name).join(", ") || "—"}
    `;
  } catch (err) {
    el.textContent = "Could not load stats.";
  }
}

// ---------------------------------------------------------------------------
// Nav wiring
// ---------------------------------------------------------------------------

document.querySelectorAll(".nav-link").forEach(link => {
  link.addEventListener("click", e => {
    e.preventDefault();
    showSection(link.dataset.section);
  });
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

showSection("library");
