// Окно фотографии: снимок целиком слева, данные и решение оператора справа.
// Данные берутся со страницы фото, поэтому всё, что работает там, работает и здесь.
(() => {
  const dialog = document.getElementById("photo-panel");
  if (!dialog || typeof dialog.showModal !== "function") return;

  const image = dialog.querySelector(".pp-image");
  const stage = dialog.querySelector(".pp-stage");
  const info = dialog.querySelector(".pp-info");
  const title = dialog.querySelector(".pp-title");
  const counter = dialog.querySelector(".pp-counter");
  const sizeLabel = dialog.querySelector(".pp-size");
  const openLink = dialog.querySelector("[data-pp-open]");
  const downloadLink = dialog.querySelector("[data-pp-download]");
  const prevButton = dialog.querySelector("[data-pp-prev]");
  const nextButton = dialog.querySelector("[data-pp-next]");
  const naturalButton = dialog.querySelector("[data-pp-natural]");
  let current = null;
  let angle = 0;
  let request = 0;

  window.nuppiPhotoPanel = true;

  const cards = () => [...document.querySelectorAll(".photo-results [data-photo-card][data-detail-url]")];

  const applyTransform = () => {
    image.style.transform = angle ? `rotate(${angle}deg)` : "";
    stage.classList.toggle("is-rotated", angle % 180 !== 0);
  };

  function absolutize(root, base) {
    root.querySelectorAll("[href], [src], [action], [formaction]").forEach((node) => {
      ["href", "src", "action", "formaction"].forEach((attribute) => {
        const value = node.getAttribute(attribute);
        if (value && !value.startsWith("#") && !/^[a-z]+:/i.test(value)) {
          node.setAttribute(attribute, new URL(value, base).href);
        }
      });
    });
  }

  async function loadInfo(card, token) {
    info.classList.add("is-loading");
    info.innerHTML = '<div class="pp-skeleton"><i></i><i></i><i></i><i></i><i></i></div>';
    try {
      const response = await fetch(card.dataset.detailUrl, { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      const html = await response.text();
      if (token !== request) return;
      const page = new DOMParser().parseFromString(html, "text/html");
      const side = page.querySelector(".detail-side");
      if (!side) throw new Error("no details");
      absolutize(side, response.url);
      side.querySelector(".btn-row")?.querySelectorAll("a[download], a[href*='download']").forEach((a) => a.remove());
      info.replaceChildren(side);
      side.dataset.photoId = card.dataset.photoId;
      // На этой странице после удаления нужно вернуться к списку.
      side.querySelectorAll('input[name="next"]').forEach((input) => { input.value = location.pathname + location.search; });
      if (window.nuppiEnhance) window.nuppiEnhance(side);
    } catch (error) {
      if (token !== request) return;
      info.innerHTML = "";
      const message = document.createElement("div");
      message.className = "empty";
      message.innerHTML = "<b>Не удалось загрузить данные</b>";
      const link = document.createElement("a");
      link.href = card.dataset.detailUrl;
      link.textContent = "Открыть страницу фотографии";
      message.append(link);
      info.append(message);
    } finally {
      if (token === request) info.classList.remove("is-loading");
    }
  }

  function show(card) {
    if (!card) return;
    current = card;
    request += 1;
    angle = 0;
    applyTransform();
    stage.classList.remove("is-natural", "is-ready");
    naturalButton.setAttribute("aria-pressed", "false");
    const list = cards();
    const index = list.indexOf(card);
    title.textContent = `Фото №${card.dataset.photoId}`;
    counter.textContent = list.length > 1 ? `${index + 1} из ${list.length}` : "";
    prevButton.disabled = index <= 0;
    nextButton.disabled = index < 0 || index >= list.length - 1;
    sizeLabel.textContent = "";
    image.removeAttribute("src");
    image.alt = `Фотография №${card.dataset.photoId}`;
    image.src = card.dataset.imageUrl;
    openLink.href = card.dataset.detailUrl;
    downloadLink.href = card.dataset.downloadUrl || card.dataset.imageUrl;
    document.querySelectorAll(".photo-card.is-current").forEach((node) => node.classList.remove("is-current"));
    card.classList.add("is-current");
    loadInfo(card, request);
    if (!dialog.open) dialog.showModal();
  }

  image.addEventListener("load", () => {
    stage.classList.add("is-ready");
    sizeLabel.textContent = `${image.naturalWidth} × ${image.naturalHeight} px`;
  });

  const step = (shift) => {
    const list = cards();
    const next = list[list.indexOf(current) + shift];
    if (next) {
      show(next);
      next.scrollIntoView({ block: "nearest" });
    }
  };

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-photo-open]");
    if (!trigger || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const card = trigger.closest("[data-photo-card][data-detail-url]");
    if (!card) return;
    event.preventDefault();
    show(card);
  });

  prevButton.addEventListener("click", () => step(-1));
  nextButton.addEventListener("click", () => step(1));
  dialog.querySelector("[data-pp-close]").addEventListener("click", () => dialog.close());
  dialog.querySelectorAll("[data-pp-rotate]").forEach((button) => button.addEventListener("click", () => {
    angle = (angle + Number(button.dataset.ppRotate)) % 360;
    applyTransform();
  }));
  naturalButton.addEventListener("click", () => {
    const natural = stage.classList.toggle("is-natural");
    naturalButton.setAttribute("aria-pressed", String(natural));
    if (natural) {
      angle = 0;
      applyTransform();
    }
  });
  dialog.querySelector("[data-pp-fullscreen]").addEventListener("click", () => {
    if (window.nuppiOpenViewer && current) {
      window.nuppiOpenViewer(current.dataset.imageUrl, current.dataset.downloadUrl, image.alt);
    }
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, .nselect, .datepick")) return;
    if (event.key === "ArrowLeft") { event.preventDefault(); step(-1); }
    if (event.key === "ArrowRight") { event.preventDefault(); step(1); }
  });
  dialog.addEventListener("close", () => {
    request += 1;
    image.removeAttribute("src");
    info.textContent = "";
    if (current) current.classList.remove("is-current");
  });
})();
