// Интерфейс панели: боковое меню, уведомления, сортировка и фильтрация таблиц,
// вид списка фото. Всё работает и без этого файла — он только добавляет удобство.
(() => {
  const root = document.documentElement;
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* приватный режим */ } },
  };

  /* ---------- Боковое меню ---------- */
  const mobileMenu = window.matchMedia("(max-width: 900px)");
  const closeSidebar = () => root.classList.remove("sidebar-open");
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-sidebar-open]")) {
      root.classList.add("sidebar-open");
      return;
    }
    if (event.target.closest("[data-sidebar-close]")) {
      closeSidebar();
      return;
    }
    if (event.target.closest("[data-sidebar-collapse]")) {
      if (mobileMenu.matches) {
        closeSidebar();
        return;
      }
      const collapsed = root.classList.toggle("sidebar-collapsed");
      store.set("nuppi.sidebar", collapsed ? "collapsed" : "open");
      return;
    }
    // На телефоне меню закрывается после перехода по ссылке.
    if (mobileMenu.matches && event.target.closest(".sidebar a")) closeSidebar();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSidebar();
  });

  /* ---------- Уведомления ---------- */
  const toasts = document.querySelector(".toasts");
  function dismiss(toast) {
    if (!toast || toast.classList.contains("is-leaving")) return;
    toast.classList.add("is-leaving");
    toast.addEventListener("animationend", () => toast.remove(), { once: true });
    setTimeout(() => toast.remove(), 600);
  }
  function scheduleDismiss(toast) {
    setTimeout(() => dismiss(toast), toast.classList.contains("error") ? 9000 : 5000);
  }
  window.nuppiToast = (message, kind = "success") => {
    if (!toasts) return;
    const toast = document.createElement("div");
    toast.className = `toast ${kind}`;
    toast.setAttribute("role", "status");
    const text = document.createElement("span");
    text.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.dataset.toastClose = "";
    close.setAttribute("aria-label", "Закрыть");
    close.textContent = "×";
    toast.append(text, close);
    toasts.append(toast);
    scheduleDismiss(toast);
  };
  document.querySelectorAll(".toast").forEach(scheduleDismiss);
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-toast-close]");
    if (button) dismiss(button.closest(".toast"));
  });

  /* ---------- Полоса загрузки при переходе на другую страницу ---------- */
  const progress = document.querySelector(".nav-progress");
  const startProgress = () => {
    if (!progress) return;
    progress.classList.remove("is-loading");
    void progress.offsetWidth;
    progress.classList.add("is-loading");
  };
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (link.target === "_blank" || link.hasAttribute("download")) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || url.pathname.endsWith(".xlsx") || url.searchParams.has("download")) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    startProgress();
  });
  document.addEventListener("submit", (event) => {
    // Формы, которые сохраняются без перезагрузки, полосу не показывают.
    setTimeout(() => {
      if (event.defaultPrevented) return;
      const action = (event.submitter && event.submitter.getAttribute("formaction")) || event.target.getAttribute("action") || "";
      if (!action.endsWith(".xlsx")) startProgress();
    }, 0);
  });
  window.addEventListener("pageshow", () => progress && progress.classList.remove("is-loading"));

  /* ---------- Меню «Ещё»: одно открытое, закрытие кликом мимо и Esc ---------- */
  const menus = () => document.querySelectorAll("details.menu[open]");
  document.addEventListener("toggle", (event) => {
    const opened = event.target;
    if (!(opened instanceof HTMLDetailsElement) || !opened.classList.contains("menu") || !opened.open) return;
    menus().forEach((menu) => { if (menu !== opened) menu.open = false; });
  }, true);
  document.addEventListener("click", (event) => {
    menus().forEach((menu) => { if (!menu.contains(event.target)) menu.open = false; });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    menus().forEach((menu) => {
      menu.open = false;
      menu.querySelector("summary")?.focus();
    });
  });

  /* ---------- Аватары: постоянный цвет для каждого имени ---------- */
  document.querySelectorAll("[data-avatar]").forEach((avatar) => {
    const name = (avatar.dataset.avatar || avatar.textContent || "").trim();
    let hash = 0;
    for (const char of name) hash = (hash * 31 + char.codePointAt(0)) % 360;
    avatar.style.setProperty("--h", String(hash));
  });

  /* ---------- Поочерёдное появление карточек ---------- */
  document.querySelectorAll("[data-stagger]").forEach((container) => {
    [...container.children].forEach((child, index) => {
      child.style.setProperty("--i", Math.min(index, 12));
    });
  });

  /* ---------- Вид списка фото: плиткой или списком ---------- */
  const viewButtons = document.querySelectorAll("[data-photo-view]");
  const applyView = (view) => {
    root.classList.toggle("photo-view-list", view === "list");
    viewButtons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.photoView === view));
    });
  };
  if (viewButtons.length) {
    applyView(store.get("nuppi.photoView") === "list" ? "list" : "grid");
    viewButtons.forEach((button) => button.addEventListener("click", () => {
      applyView(button.dataset.photoView);
      store.set("nuppi.photoView", button.dataset.photoView);
    }));
  }

  /* ---------- Запоминаем, раскрыты ли фильтры ---------- */
  document.querySelectorAll("details[data-remember]").forEach((details) => {
    const key = details.dataset.remember;
    if (!details.open && store.get(key) === "open") details.open = true;
    details.addEventListener("toggle", () => store.set(key, details.open ? "open" : "closed"));
  });

  /* ---------- Сортировка таблиц по клику на заголовок ---------- */
  const collator = new Intl.Collator("ru", { numeric: true, sensitivity: "base" });
  const cellValue = (row, index) => {
    const cell = row.cells[index];
    if (!cell) return "";
    return (cell.dataset.value ?? cell.textContent).trim();
  };
  document.querySelectorAll("table[data-sortable]").forEach((table) => {
    const body = table.tBodies[0];
    if (!body) return;
    table.querySelectorAll("th[data-sort]").forEach((header) => {
      const columnIndex = [...header.parentElement.children].indexOf(header);
      header.tabIndex = 0;
      header.setAttribute("role", "button");
      header.title = "Нажмите, чтобы отсортировать";
      const sort = () => {
        const ascending = header.getAttribute("aria-sort") !== "ascending";
        table.querySelectorAll("th[aria-sort]").forEach((th) => th.removeAttribute("aria-sort"));
        header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
        const numeric = header.dataset.sort === "num";
        const rows = [...body.rows];
        rows.sort((a, b) => {
          const left = cellValue(a, columnIndex);
          const right = cellValue(b, columnIndex);
          const result = numeric
            ? (Number.parseFloat(left.replace(/\s/g, "")) || 0) - (Number.parseFloat(right.replace(/\s/g, "")) || 0)
            : collator.compare(left, right);
          return ascending ? result : -result;
        });
        rows.forEach((row) => {
          row.classList.remove("row-moved");
          void row.offsetWidth;
          row.classList.add("row-moved");
          body.append(row);
        });
      };
      header.addEventListener("click", sort);
      header.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          sort();
        }
      });
    });
  });

  /* ---------- Фильтрация таблиц и списков без перезагрузки ---------- */
  const normalize = (value) => value.toLocaleLowerCase("ru").replace(/ё/g, "е").trim();

  document.querySelectorAll("[data-table-filter]").forEach((tools) => {
    const table = document.querySelector(tools.dataset.tableFilter);
    if (!table) return;
    const wrap = table.closest(".table-wrap") || table.parentElement;
    const empty = wrap.querySelector(".filter-empty");
    const counter = tools.querySelector("[data-filter-count]");
    const items = [...table.querySelectorAll("[data-filter-item]")];
    const controls = [...tools.querySelectorAll("input, select")];
    const apply = () => {
      const query = normalize(tools.querySelector('[name="q"]')?.value || "");
      let shown = 0;
      items.forEach((item) => {
        let visible = !query || normalize(item.textContent).includes(query);
        controls.forEach((control) => {
          if (control.name === "q" || !control.value) return;
          if (item.dataset[control.name] !== control.value) visible = false;
        });
        item.hidden = !visible;
        if (visible) shown += 1;
      });
      if (empty) empty.hidden = shown !== 0;
      if (counter) counter.textContent = shown === items.length ? `Всего: ${items.length}` : `Показано ${shown} из ${items.length}`;
    };
    controls.forEach((control) => control.addEventListener("input", apply));
    apply();
  });

  document.querySelectorAll("[data-filter-list]").forEach((input) => {
    const list = document.querySelector(input.dataset.filterList);
    if (!list) return;
    const items = [...list.querySelectorAll("[data-filter-item]")];
    const empty = list.querySelector(".filter-empty");
    input.addEventListener("input", () => {
      const query = normalize(input.value);
      let shown = 0;
      items.forEach((item) => {
        const visible = !query || normalize(item.textContent).includes(query);
        item.hidden = !visible;
        if (visible) shown += 1;
      });
      if (empty) empty.hidden = shown !== 0;
    });
  });
})();
