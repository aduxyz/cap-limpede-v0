// ==UserScript==
// @name         Cap Limpede CMS helper
// @namespace    cap-limpede
// @version      0.1.5
// @description  Injecteaza Cap Limpede in CMS-ul vechi fara modificari in aplicatia existenta.
// @match        *://data2.1616.ro/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function () {
  "use strict";

  const BACKEND_URL = "http://127.0.0.1:8000/api/review";
  const MODELS = ["gpt-5.4-nano"];
  const LEVELS = [1, 2];
  const pageWindow = typeof unsafeWindow !== "undefined" ? unsafeWindow : window;
  const TEXT_FIELD_TARGETS = [
    { key: "title", label: "Titlu", selector: 'textarea[name="default_title"]' },
    { key: "description", label: "Descriere", selector: 'textarea[name="default_description"]' }
  ];
  const CKEDITOR_FIELD_TARGETS = [
    { key: "article_body", label: "Text articol", prefix: "details", wrapperSelector: "#wrapper_details" },
    { key: "lead", label: "Lead", prefix: "lead", wrapperSelector: "#wrapper_lead" }
  ];

  const state = { fields: [], reviews: [], busy: false };

  function injectCss() {
    const style = document.createElement("style");
    style.textContent = `
      #cap-limpede-cms-button{position:fixed;right:18px;bottom:18px;z-index:2147483647;border:1px solid #1d4ed8;background:#2563eb;color:#fff;border-radius:999px;padding:10px 16px;font:700 14px/1.2 Arial,sans-serif;box-shadow:0 10px 28px rgba(0,0,0,.24);cursor:pointer}
      #cap-limpede-cms-button[disabled]{opacity:.7;cursor:wait}
      #cap-limpede-cms-overlay{position:fixed;inset:0;z-index:2147483646;background:rgba(15,23,42,.42);display:none;align-items:stretch;justify-content:flex-end;font:14px/1.45 Arial,sans-serif}
      #cap-limpede-cms-overlay.visible{display:flex}
      .cl-panel{width:min(720px,96vw);height:100vh;background:#f8fafc;color:#1f2937;box-shadow:-16px 0 40px rgba(15,23,42,.22);display:flex;flex-direction:column}
      .cl-head{padding:16px 18px;background:#fff;border-bottom:1px solid #dbe3ef;display:flex;align-items:center;gap:12px}.cl-head h2{margin:0;font-size:18px;flex:1}
      .cl-head button,.cl-footer button,.cl-suggestion-actions button{border:1px solid #d8e0ea;background:#fff;color:#1f2937;border-radius:999px;padding:8px 12px;font:700 13px/1.2 Arial,sans-serif;cursor:pointer}
      .cl-head button.primary,.cl-footer button.primary,.cl-suggestion-actions button.primary{background:#2563eb;border-color:#2563eb;color:#fff}
      .cl-body{overflow:auto;padding:16px 18px 90px}.cl-status{color:#64748b;margin-bottom:12px}.cl-field{background:#fff;border:1px solid #dbe3ef;border-radius:14px;margin-bottom:14px;overflow:hidden}
      .cl-field h3{margin:0;padding:12px 14px;background:#eef4ff;border-bottom:1px solid #dbe3ef;font-size:15px}.cl-suggestion{padding:12px 14px;border-bottom:1px solid #edf2f7}.cl-suggestion:last-child{border-bottom:0}
      .cl-level{color:#64748b;font-weight:700;margin-bottom:8px}.cl-pair{border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;margin-bottom:8px}.cl-original,.cl-proposed{padding:10px;white-space:pre-wrap}
      .cl-original{background:#f8fafc;color:#b91c1c;font-style:italic}.cl-proposed{background:#fff;font-weight:700}.cl-reason{color:#475569;margin-bottom:10px}.cl-suggestion-actions{display:flex;gap:8px}
      .cl-field-preview{margin:12px 14px;padding:10px;border:1px dashed #cbd5e1;border-radius:10px;background:#f8fafc;color:#475569;white-space:pre-wrap;max-height:220px;overflow:auto}
      .cl-field-preview strong{display:block;color:#1f2937;margin-bottom:6px}
      .cl-suggestion[data-status="approved"]{outline:2px solid rgba(22,163,74,.28)}.cl-suggestion[data-status="rejected"]{opacity:.55}
      .cl-footer{position:fixed;right:0;bottom:0;width:min(720px,96vw);background:#fff;border-top:1px solid #dbe3ef;padding:12px 18px;display:flex;gap:10px;justify-content:flex-end}
    `;
    document.documentElement.appendChild(style);
  }

  function escapeHtml(value) {
    return String(value || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function normalizeText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function readableText(value) {
    const raw = String(value || "");
    if (!raw.includes("<")) return normalizeText(raw);
    const box = document.createElement("div");
    box.innerHTML = raw;
    return normalizeText(box.textContent || box.innerText || raw);
  }

  function firstMeaningfulValue(values) {
    return values.find((value) => readableText(value)) || "";
  }

  function currentItemId() {
    const params = new URLSearchParams(window.location.search);
    return params.get("id") || (document.querySelector(".dms-form-item-id") || {}).textContent || "";
  }

  function getTextField(target) {
    const element = document.querySelector(target.selector);
    if (!element) return null;
    const value = element.value || "";
    if (!normalizeText(value)) return null;
    return { key: target.key, label: target.label, type: "text", element, sourceEl: element, instance: null, iframe: null, original: value, value };
  }

  function findCkEditorSource(target) {
    const itemId = currentItemId().trim();
    if (itemId) {
      const exactByItem = document.getElementById(`${target.prefix}_${itemId}`);
      if (exactByItem) return exactByItem;
    }
    const exact = document.querySelector(`${target.wrapperSelector} textarea[id^="${target.prefix}_"]`);
    if (exact) return exact;
    return Array.from(document.querySelectorAll("textarea[id], textarea[title]")).find((element) => {
      const id = element.id || "";
      const title = element.title || "";
      return id.includes(target.prefix) || title.includes(target.prefix);
    }) || null;
  }

  function findCkEditorInstance(target, sourceEl) {
    const instances = pageWindow.CKEDITOR && pageWindow.CKEDITOR.instances ? pageWindow.CKEDITOR.instances : {};
    if (sourceEl && instances[sourceEl.id]) return instances[sourceEl.id];
    const match = Object.entries(instances).find(([name]) => name.includes(target.prefix));
    return match ? match[1] : null;
  }

  function findCkEditorIframe(target, sourceEl) {
    const sourceId = sourceEl && sourceEl.id;
    if (sourceId) {
      const iframe = document.querySelector(`#cke_contents_${CSS.escape(sourceId)} iframe`);
      if (iframe) return iframe;
    }
    return document.querySelector(`${target.wrapperSelector} iframe`) ||
      Array.from(document.querySelectorAll("iframe[title*='Rich text editor']")).find((iframe) => (iframe.title || "").includes(target.prefix)) ||
      null;
  }

  function readIframeHtml(iframe) {
    try {
      return iframe && iframe.contentDocument && iframe.contentDocument.body ? iframe.contentDocument.body.innerHTML : "";
    } catch (_) {
      return "";
    }
  }

  function writeIframeHtml(iframe, value) {
    try {
      const body = iframe && iframe.contentDocument && iframe.contentDocument.body;
      if (!body) return;
      body.innerHTML = value;
      body.dispatchEvent(new Event("input", { bubbles: true }));
      body.dispatchEvent(new Event("change", { bubbles: true }));
      body.dispatchEvent(new Event("blur", { bubbles: true }));
    } catch (_) {}
  }

  function getCkEditorField(target) {
    const sourceEl = findCkEditorSource(target);
    const instance = findCkEditorInstance(target, sourceEl);
    const iframe = findCkEditorIframe(target, sourceEl);
    const value = firstMeaningfulValue([
      instance && instance.getData ? instance.getData() : "",
      sourceEl ? sourceEl.value : "",
      readIframeHtml(iframe)
    ]);
    if (!readableText(value)) return null;
    return { key: target.key, label: target.label, type: "ckeditor", element: sourceEl, sourceEl, instance, iframe, original: value, value };
  }

  function extractFields() {
    return [
      ...TEXT_FIELD_TARGETS.map(getTextField),
      ...CKEDITOR_FIELD_TARGETS.map(getCkEditorField)
    ].filter(Boolean);
  }

  function requestReview(field) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: BACKEND_URL,
        headers: { "Content-Type": "application/json" },
        data: JSON.stringify({ source: field.value, models: MODELS, levels: LEVELS }),
        onload: (response) => {
          try {
            const data = JSON.parse(response.responseText || "{}");
            if (response.status < 200 || response.status >= 300) reject(new Error(data.error || `HTTP ${response.status}`));
            else resolve(data);
          } catch (error) {
            reject(error);
          }
        },
        onerror: () => reject(new Error("Nu m-am putut conecta la backend-ul Cap Limpede."))
      });
    });
  }

  function applySuggestion(text, suggestion) {
    const original = suggestion.original || "";
    const proposed = suggestion.proposed || "";
    if (!original || !text.includes(original)) return text;
    return text.replace(original, proposed);
  }

  function applyApprovedToField(field, suggestions) {
    let nextValue = field.original;
    suggestions.filter((item) => item.status === "approved").forEach((item) => {
      nextValue = applySuggestion(nextValue, item);
    });
    field.value = nextValue;

    if (field.instance && field.instance.setData) {
      field.instance.setData(nextValue);
      if (field.instance.fire) field.instance.fire("change");
    }
    if (field.sourceEl || field.element) {
      const element = field.sourceEl || field.element;
      if (field.type === "html") element.innerHTML = nextValue;
      else element.value = nextValue;
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
      element.dispatchEvent(new Event("blur", { bubbles: true }));
    }
    if (field.iframe) writeIframeHtml(field.iframe, nextValue);
  }

  function renderOverlay() {
    const overlay = document.getElementById("cap-limpede-cms-overlay");
    const body = overlay.querySelector(".cl-body");
    const total = state.reviews.reduce((sum, review) => sum + review.suggestions.length, 0);
    body.innerHTML = `
      <div class="cl-status">${state.fields.length} câmpuri detectate. ${total} sugestii găsite.</div>
      ${state.reviews.map((review, fieldIndex) => `
        <section class="cl-field">
          <h3>${escapeHtml(review.field.label)}</h3>
          <div class="cl-field-preview"><strong>Text detectat (${escapeHtml(review.field.key)} · ${escapeHtml(review.field.type)} · ${readableText(review.field.value).length} caractere)</strong>${escapeHtml(review.field.value || "")}</div>
          ${review.suggestions.length ? review.suggestions.map((suggestion, suggestionIndex) => `
            <article class="cl-suggestion" data-field-index="${fieldIndex}" data-suggestion-index="${suggestionIndex}" data-status="${suggestion.status}">
              <div class="cl-level">Nivel ${suggestion.level}: ${escapeHtml(suggestion.reason || "")}</div>
              <div class="cl-pair"><div class="cl-original">${escapeHtml(suggestion.original || "")}</div><div class="cl-proposed">${escapeHtml(suggestion.proposed || "")}</div></div>
              <div class="cl-suggestion-actions"><button class="primary" data-action="approve">Approve</button><button data-action="reject">Reject</button><button data-action="pending">Undo</button></div>
            </article>
          `).join("") : `<div class="cl-suggestion">Nu sunt sugestii pentru câmpul acesta.</div>`}
        </section>
      `).join("")}
    `;
    body.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const row = button.closest(".cl-suggestion");
        const fieldIndex = Number(row.dataset.fieldIndex);
        const suggestionIndex = Number(row.dataset.suggestionIndex);
        state.reviews[fieldIndex].suggestions[suggestionIndex].status = button.dataset.action === "approve" ? "approved" : button.dataset.action === "reject" ? "rejected" : "pending";
        applyApprovedToField(state.reviews[fieldIndex].field, state.reviews[fieldIndex].suggestions);
        renderOverlay();
      });
    });
  }

  function openOverlay() {
    document.getElementById("cap-limpede-cms-overlay").classList.add("visible");
  }

  function closeOverlay() {
    document.getElementById("cap-limpede-cms-overlay").classList.remove("visible");
  }

  async function runReview() {
    if (state.busy) return;
    state.busy = true;
    const button = document.getElementById("cap-limpede-cms-button");
    button.disabled = true;
    button.textContent = "Analizez...";
    try {
      state.fields = extractFields();
      if (!state.fields.length) {
        alert("Nu am gasit campuri de articol in pagina curenta.");
        return;
      }
      state.reviews = await Promise.all(state.fields.map(async (field) => {
        const review = await requestReview(field);
        const firstRun = (review.runs && review.runs[0]) || {};
        return { field, suggestions: (firstRun.suggestions || []).map((suggestion) => ({ ...suggestion, status: "pending" })) };
      }));
      renderOverlay();
      openOverlay();
    } catch (error) {
      alert(error.message || "Nu am putut procesa articolul.");
    } finally {
      state.busy = false;
      button.disabled = false;
      button.textContent = "Cap Limpede";
    }
  }

  function applyAll() {
    state.reviews.forEach((review) => applyApprovedToField(review.field, review.suggestions));
    closeOverlay();
  }

  function bootstrap() {
    injectCss();
    const button = document.createElement("button");
    button.id = "cap-limpede-cms-button";
    button.type = "button";
    button.textContent = "Cap Limpede";
    button.addEventListener("click", runReview);
    document.body.appendChild(button);

    const overlay = document.createElement("div");
    overlay.id = "cap-limpede-cms-overlay";
    overlay.innerHTML = `<div class="cl-panel"><div class="cl-head"><h2>Cap Limpede</h2><button type="button" id="cl-close">Inchide</button></div><div class="cl-body"></div><div class="cl-footer"><button type="button" id="cl-apply" class="primary">Aplica aprobate</button></div></div>`;
    document.body.appendChild(overlay);
    overlay.querySelector("#cl-close").addEventListener("click", closeOverlay);
    overlay.querySelector("#cl-apply").addEventListener("click", applyAll);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap, { once: true });
  } else {
    bootstrap();
  }
})();
