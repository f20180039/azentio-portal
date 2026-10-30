// Replaces native <select> UI with a themed listbox. The real <select> stays in the form (visually hidden),
// so submission, server validation and no-JS fallback are unchanged. Keyboard: arrows, Home/End, Enter/Space,
// Escape, Tab and type-ahead. The popup is position:fixed so tables with overflow never clip it.
(function () {
  "use strict";
  let open = null; // {wrap, list, button, select, items, active}
  let uid = 0;

  const chevron = '<svg class="dd-chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const tick = '<svg class="dd-tick" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  function labelFor(select) {
    if (select.id) {
      const l = document.querySelector('label[for="' + select.id + '"]');
      if (l) return l;
    }
    return null;
  }

  function enhance(select) {
    if (select.multiple || select.dataset.ddDone) return;
    select.dataset.ddDone = "1";
    const wrap = document.createElement("div");
    wrap.className = "dd" + (select.closest(".viewas") ? " dd-dark" : "");
    if (select.style.maxWidth) wrap.style.maxWidth = select.style.maxWidth;
    if (select.style.width === "auto") wrap.classList.add("dd-auto");
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    select.classList.add("dd-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");

    const button = document.createElement("button");
    button.type = "button";
    button.className = "dd-btn";
    button.id = "dd-btn-" + (++uid);
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    const label = labelFor(select);
    if (label) {
      label.id = label.id || "dd-label-" + uid;
      button.setAttribute("aria-labelledby", label.id + " " + button.id);
      label.addEventListener("click", function (e) { e.preventDefault(); button.focus(); });
    } else if (select.getAttribute("aria-label")) {
      button.setAttribute("aria-label", select.getAttribute("aria-label"));
    }
    wrap.appendChild(button);

    function sync() {
      const opt = select.options[select.selectedIndex];
      button.innerHTML = '<span class="dd-value">' + escapeHtml(opt ? opt.text : "") + "</span>" + chevron;
    }
    sync();
    select.addEventListener("change", sync);

    button.addEventListener("click", function () { open && open.select === select ? close(true) : show(select, wrap, button); });
    button.addEventListener("keydown", function (e) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
        e.preventDefault();
        show(select, wrap, button);
      } else if (e.key.length === 1 && /\S/.test(e.key)) {
        typeAhead(select, e.key, true);
      }
    });
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; });
  }

  function show(select, wrap, button) {
    close(false);
    const list = document.createElement("ul");
    list.className = "dd-list" + (wrap.classList.contains("dd-dark") ? " dd-dark" : "");
    list.setAttribute("role", "listbox");
    list.id = button.id + "-list";
    list.tabIndex = -1;
    const lab = button.getAttribute("aria-labelledby");
    if (lab) list.setAttribute("aria-labelledby", lab.split(" ")[0]);
    const items = [];
    Array.from(select.children).forEach(function (node) {
      if (node.tagName === "OPTGROUP") {
        const head = document.createElement("li");
        head.className = "dd-group";
        head.setAttribute("role", "presentation");
        head.textContent = node.label;
        list.appendChild(head);
        Array.from(node.children).forEach(function (o) { items.push(addOption(list, o, select)); });
      } else {
        items.push(addOption(list, node, select));
      }
    });
    document.body.appendChild(list);
    open = { wrap: wrap, list: list, button: button, select: select, items: items, active: Math.max(0, select.selectedIndex) };
    button.setAttribute("aria-expanded", "true");
    button.setAttribute("aria-controls", list.id);
    wrap.classList.add("is-open");
    place();
    setActive(open.active);
    list.focus({ preventScroll: true });
  }

  function addOption(list, opt, select) {
    const li = document.createElement("li");
    li.className = "dd-opt";
    li.setAttribute("role", "option");
    li.id = "dd-opt-" + (++uid);
    li.innerHTML = tick + '<span>' + escapeHtml(opt.text) + "</span>";
    const selected = opt.index === select.selectedIndex;
    li.setAttribute("aria-selected", selected ? "true" : "false");
    if (opt.disabled) li.setAttribute("aria-disabled", "true");
    li.addEventListener("mousemove", function () { if (open) setActive(open.items.indexOf(li)); });
    li.addEventListener("click", function () { choose(opt.index); });
    li.dataset.index = opt.index;
    list.appendChild(li);
    return li;
  }

  function place() {
    if (!open) return;
    const r = open.button.getBoundingClientRect();
    const list = open.list;
    list.style.minWidth = r.width + "px";
    list.style.left = Math.max(8, Math.min(r.left, window.innerWidth - list.offsetWidth - 8)) + "px";
    const below = window.innerHeight - r.bottom;
    const h = Math.min(list.scrollHeight, parseFloat(getComputedStyle(list).maxHeight) || 320);
    if (below < h + 12 && r.top > below) {
      list.style.top = Math.max(8, r.top - h - 6) + "px";
      list.classList.add("up");
    } else {
      list.style.top = r.bottom + 6 + "px";
      list.classList.remove("up");
    }
  }

  function setActive(i) {
    if (!open || !open.items.length) return;
    open.active = Math.max(0, Math.min(i, open.items.length - 1));
    open.items.forEach(function (li, n) { li.classList.toggle("is-active", n === open.active); });
    const li = open.items[open.active];
    open.list.setAttribute("aria-activedescendant", li.id);
    li.scrollIntoView({ block: "nearest" });
  }

  function choose(index) {
    if (!open) return;
    const select = open.select;
    const opt = select.options[index];
    if (!opt || opt.disabled) return;
    const changed = select.selectedIndex !== index;
    select.selectedIndex = index;
    close(true);
    if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function close(refocus) {
    if (!open) return;
    const o = open;
    open = null;
    o.list.remove();
    o.button.setAttribute("aria-expanded", "false");
    o.wrap.classList.remove("is-open");
    if (refocus) o.button.focus({ preventScroll: true });
  }

  let typed = "", typedAt = 0;
  function typeAhead(select, ch, commit) {
    const now = Date.now();
    typed = now - typedAt > 700 ? ch.toLowerCase() : typed + ch.toLowerCase();
    typedAt = now;
    const opts = Array.from(select.options);
    const hit = opts.find(function (o) { return !o.disabled && o.text.toLowerCase().startsWith(typed); });
    if (!hit) return;
    if (commit && !open) {
      if (select.selectedIndex !== hit.index) {
        select.selectedIndex = hit.index;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
    } else if (open) {
      setActive(open.items.findIndex(function (li) { return Number(li.dataset.index) === hit.index; }));
    }
  }

  document.addEventListener("keydown", function (e) {
    if (!open || !open.list.contains(document.activeElement)) return;
    const k = e.key;
    if (k === "ArrowDown") { e.preventDefault(); setActive(open.active + 1); }
    else if (k === "ArrowUp") { e.preventDefault(); setActive(open.active - 1); }
    else if (k === "Home") { e.preventDefault(); setActive(0); }
    else if (k === "End") { e.preventDefault(); setActive(open.items.length - 1); }
    else if (k === "Enter" || k === " ") { e.preventDefault(); choose(Number(open.items[open.active].dataset.index)); }
    else if (k === "Escape") { e.preventDefault(); close(true); }
    else if (k === "Tab") { close(false); }
    else if (k.length === 1 && /\S/.test(k)) { typeAhead(open.select, k, false); }
  });
  document.addEventListener("mousedown", function (e) {
    if (open && !open.list.contains(e.target) && !open.wrap.contains(e.target)) close(false);
  });
  window.addEventListener("resize", function () { close(false); });
  window.addEventListener("scroll", function (e) { if (open && !open.list.contains(e.target)) close(false); }, true);

  function init() { document.querySelectorAll("select").forEach(enhance); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
