// Hover and keyboard-focus tooltip for the reliability diagram. Every value it shows is also
// in the table view beneath the chart, so the tooltip enhances rather than gates.
(() => {
  const hits = document.querySelectorAll("svg.reliability .hit");
  if (!hits.length) return;

  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.hidden = true;
  document.body.append(tip);

  const show = (el) => {
    const value = document.createElement("strong");
    value.textContent = `Accuracy ${el.dataset.accuracy}`;

    const row = document.createElement("div");
    row.className = "row";
    const key = document.createElement("span");
    key.className = "key";
    key.style.background = `var(${el.dataset.colorVar})`;
    const label = document.createElement("span");
    label.textContent = `${el.dataset.series} · confidence ${el.dataset.confidence}`;
    row.append(key, label);

    const count = document.createElement("div");
    count.className = "row";
    count.textContent = `${el.dataset.count} runs in bin ${el.dataset.bin}`;

    tip.replaceChildren(value, row, count);
    tip.hidden = false;

    const box = el.getBoundingClientRect();
    const width = tip.offsetWidth;
    let left = box.right + 8;
    if (left + width > window.innerWidth - 8) left = box.left - width - 8;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, box.top - 4)}px`;
  };
  const hide = () => { tip.hidden = true; };

  hits.forEach((el) => {
    el.addEventListener("pointerenter", () => show(el));
    el.addEventListener("focus", () => show(el));
    el.addEventListener("pointerleave", hide);
    el.addEventListener("blur", hide);
  });
})();
