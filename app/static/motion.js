// Cards in grids rise into view as they are scrolled to, staggered across each row. Without IntersectionObserver, or with
// reduced motion, nothing is hidden and nothing moves.
(() => {
  if (!("IntersectionObserver" in window) || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const show = (el) => { el.classList.add("in"); io.unobserve(el); };
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) show(e.target);
  }, { rootMargin: "0px 0px -6% 0px" });
  // a jump (anchor link, End key) skips cards without them ever intersecting; show anything already scrolled past
  let queued = false;
  addEventListener("scroll", () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      for (const el of document.querySelectorAll(".reveal:not(.in)")) if (el.getBoundingClientRect().bottom < 0) show(el);
    });
  }, { passive: true });
  const settle = (e) => { if (e.propertyName === "opacity") e.target.classList.remove("reveal", "in"); };
  for (const group of document.querySelectorAll(".grid, .kpis, .personas")) {
    const items = [...group.children];
    if (items.length < 2) continue;
    const cols = items.filter((el) => el.offsetTop === items[0].offsetTop).length || 1;
    items.forEach((el, i) => {
      el.style.setProperty("--reveal-delay", `${(i % cols) * 90}ms`);
      el.classList.add("reveal");
      el.addEventListener("transitionend", settle);  // hand the card back to its own hover transitions
      io.observe(el);
    });
  }
})();
