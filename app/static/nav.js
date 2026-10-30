// Below 1024px the site menu collapses behind the Menu button; without JavaScript it simply wraps.
(() => {
  const top = document.querySelector(".top"), btn = document.getElementById("nav-toggle");
  if (!top || !btn) return;
  top.classList.add("has-toggle");
  const set = (open) => { top.classList.toggle("open", open); btn.setAttribute("aria-expanded", String(open)); };
  btn.addEventListener("click", () => set(btn.getAttribute("aria-expanded") !== "true"));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && top.classList.contains("open")) { set(false); btn.focus(); }
  });
})();
