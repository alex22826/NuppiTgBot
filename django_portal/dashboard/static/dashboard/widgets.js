// Свои выпадающие списки и календари в стиле сайта.
// Обычные <select> и <input type="date"> остаются в форме и отправляются как
// раньше — скрипт только рисует поверх них удобный интерфейс.
(() => {
  const MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
  const MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
  const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
  const openPopovers = new Set();

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const chevron = () => {
    const span = el("span", "nselect-chevron");
    span.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-chevron"/></svg>';
    return span;
  };
  const fire = (node, ...types) => types.forEach((type) => node.dispatchEvent(new Event(type, { bubbles: true })));

  function closeAll(except) {
    openPopovers.forEach((popover) => { if (popover !== except) popover.close(); });
  }
  document.addEventListener("click", (event) => {
    // Клик по дню перерисовывает календарь: нажатая кнопка уже удалена из
    // страницы, и это не «клик мимо».
    if (!event.target.isConnected) return;
    openPopovers.forEach((popover) => { if (!popover.root.contains(event.target)) popover.close(); });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") openPopovers.forEach((popover) => popover.close(true));
  });

  function placeMenu(root, menu) {
    menu.classList.remove("drop-up", "align-right");
    const box = root.getBoundingClientRect();
    const spaceBelow = window.innerHeight - box.bottom;
    if (spaceBelow < Math.min(menu.offsetHeight, 340) + 16 && box.top > spaceBelow) menu.classList.add("drop-up");
    if (box.left + menu.offsetWidth > window.innerWidth - 12) menu.classList.add("align-right");
  }

  /* ================= Выпадающий список ================= */

  function enhanceSelect(select) {
    if (select.dataset.enhanced || select.multiple || select.dataset.native !== undefined) return;
    select.dataset.enhanced = "1";
    const root = el("div", "nselect");
    const host = select.closest(".select-inline");
    if (host) root.classList.add("is-inline");
    const leading = host && host.querySelector(":scope > .icon");
    select.parentNode.insertBefore(root, select);
    root.append(select);
    select.classList.add("nselect-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");

    const trigger = el("button", "nselect-trigger");
    trigger.type = "button";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    const label = select.closest("label")?.querySelector(":scope > span:not(.sr-only)")?.textContent
      || select.closest("label")?.querySelector(".sr-only")?.textContent || "";
    if (label) trigger.setAttribute("aria-label", label.trim());
    if (leading) {
      trigger.append(leading.cloneNode(true));
      leading.remove();
      root.classList.add("has-icon");
    }
    const value = el("span", "nselect-value");
    trigger.append(value, chevron());
    const menu = el("div", "nselect-menu");
    menu.setAttribute("role", "listbox");
    root.append(trigger, menu);

    let search = null;
    let items = [];
    let active = -1;

    const sync = () => {
      const option = select.selectedOptions[0];
      value.textContent = option ? option.textContent : "";
      root.classList.toggle("is-placeholder", !select.value);
      items.forEach((item) => item.setAttribute("aria-selected", String(item.dataset.value === select.value)));
    };

    const build = () => {
      menu.textContent = "";
      items = [];
      const optionCount = select.querySelectorAll("option").length;
      if (optionCount > 8) {
        const box = el("div", "nselect-search");
        box.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-search"/></svg>';
        search = el("input");
        search.type = "search";
        search.placeholder = "Найти…";
        search.setAttribute("aria-label", "Поиск в списке");
        box.append(search);
        menu.append(box);
        search.addEventListener("input", () => filter(search.value));
        search.addEventListener("keydown", onKey);
      }
      const list = el("div", "nselect-list");
      const addOption = (option) => {
        const item = el("button", "nselect-option");
        item.type = "button";
        item.dataset.value = option.value;
        item.setAttribute("role", "option");
        item.innerHTML = '<svg class="icon check" aria-hidden="true"><use href="#i-check"/></svg>';
        item.append(el("span", "", option.textContent));
        if (option.disabled) item.disabled = true;
        item.addEventListener("click", () => choose(option.value));
        item.addEventListener("mousemove", () => highlight(items.indexOf(item)));
        list.append(item);
        items.push(item);
      };
      [...select.children].forEach((child) => {
        if (child.tagName === "OPTGROUP") {
          const group = el("div", "nselect-group", child.label);
          list.append(group);
          [...child.children].forEach(addOption);
        } else if (child.tagName === "OPTION") {
          addOption(child);
        }
      });
      const empty = el("div", "nselect-empty", "Ничего не найдено");
      empty.hidden = true;
      list.append(empty);
      menu.append(list);
      sync();
    };

    const filter = (text) => {
      const query = text.toLocaleLowerCase("ru").replace(/ё/g, "е").trim();
      let shown = 0;
      items.forEach((item) => {
        const visible = !query || item.textContent.toLocaleLowerCase("ru").replace(/ё/g, "е").includes(query);
        item.hidden = !visible;
        if (visible) shown += 1;
      });
      menu.querySelectorAll(".nselect-group").forEach((group) => {
        let next = group.nextElementSibling;
        let any = false;
        while (next && !next.classList.contains("nselect-group")) {
          if (next.classList.contains("nselect-option") && !next.hidden) any = true;
          next = next.nextElementSibling;
        }
        group.hidden = !any;
      });
      menu.querySelector(".nselect-empty").hidden = shown !== 0;
      highlight(items.findIndex((item) => !item.hidden));
    };

    const highlight = (index) => {
      items.forEach((item) => item.classList.remove("is-active"));
      active = index;
      const item = items[index];
      if (item) {
        item.classList.add("is-active");
        item.scrollIntoView({ block: "nearest" });
      }
    };

    const visibleItems = () => items.filter((item) => !item.hidden && !item.disabled);

    const popover = {
      root,
      open() {
        closeAll(popover);
        build();
        root.classList.add("is-open");
        trigger.setAttribute("aria-expanded", "true");
        openPopovers.add(popover);
        placeMenu(root, menu);
        highlight(items.findIndex((item) => item.dataset.value === select.value));
        if (search) setTimeout(() => search.focus(), 0);
      },
      close(focus) {
        root.classList.remove("is-open");
        trigger.setAttribute("aria-expanded", "false");
        openPopovers.delete(popover);
        if (focus) trigger.focus();
      },
    };

    const choose = (newValue) => {
      const changed = select.value !== newValue;
      select.value = newValue;
      sync();
      popover.close(true);
      if (changed) fire(select, "input", "change");
    };

    function onKey(event) {
      const visible = visibleItems();
      const position = visible.indexOf(items[active]);
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (!root.classList.contains("is-open")) return popover.open();
        highlight(items.indexOf(visible[Math.min(position + 1, visible.length - 1)]));
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        highlight(items.indexOf(visible[Math.max(position - 1, 0)]));
      } else if (event.key === "Enter") {
        if (!root.classList.contains("is-open")) return;
        event.preventDefault();
        if (items[active]) choose(items[active].dataset.value);
      } else if (event.key === "Tab") {
        popover.close();
      }
    }

    trigger.addEventListener("click", () => (root.classList.contains("is-open") ? popover.close() : popover.open()));
    trigger.addEventListener("keydown", (event) => {
      if (event.key === " " && !root.classList.contains("is-open")) {
        event.preventDefault();
        popover.open();
        return;
      }
      onKey(event);
    });
    select.addEventListener("change", sync);
    // Другие скрипты сайта иногда меняют значение напрямую (сброс зависимого поля).
    new MutationObserver(build).observe(select, { childList: true, subtree: true });
    build();
  }

  /* ================= Календарь ================= */

  const iso = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  const parse = (value) => {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    return match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : null;
  };
  const human = (date) => `${String(date.getDate()).padStart(2, "0")}.${String(date.getMonth() + 1).padStart(2, "0")}.${date.getFullYear()}`;
  const humanLong = (date) => `${date.getDate()} ${MONTHS_GEN[date.getMonth()]} ${date.getFullYear()}`;
  const today = () => { const now = new Date(); return new Date(now.getFullYear(), now.getMonth(), now.getDate()); };
  const addDays = (date, days) => new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
  const sameDay = (a, b) => a && b && a.getTime() === b.getTime();

  function calendarGrid(month, { start, end, single, onPick }) {
    const grid = el("div", "cal-grid");
    WEEKDAYS.forEach((day) => grid.append(el("span", "cal-weekday", day)));
    const first = new Date(month.getFullYear(), month.getMonth(), 1);
    const offset = (first.getDay() + 6) % 7;
    const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
    for (let i = 0; i < offset; i += 1) grid.append(el("span", "cal-blank"));
    const now = today();
    for (let day = 1; day <= days; day += 1) {
      const date = new Date(month.getFullYear(), month.getMonth(), day);
      const button = el("button", "cal-day", String(day));
      button.type = "button";
      button.dataset.date = iso(date);
      button.setAttribute("aria-label", humanLong(date));
      if (sameDay(date, now)) button.classList.add("is-today");
      if (single) {
        if (sameDay(date, start)) button.classList.add("is-selected", "is-start", "is-end");
      } else {
        if (sameDay(date, start)) button.classList.add("is-selected", "is-start");
        if (sameDay(date, end)) button.classList.add("is-selected", "is-end");
        if (start && end && date > start && date < end) button.classList.add("in-range");
      }
      button.addEventListener("click", () => onPick(date));
      grid.append(button);
    }
    return grid;
  }

  function makeCalendarPopover({ root, trigger, render, onOpen }) {
    const panel = el("div", "cal-popover");
    panel.setAttribute("role", "dialog");
    root.append(panel);
    const popover = {
      root,
      open() {
        closeAll(popover);
        if (onOpen) onOpen();
        render(panel);
        root.classList.add("is-open");
        trigger.setAttribute("aria-expanded", "true");
        openPopovers.add(popover);
        placeMenu(root, panel);
      },
      close(focus) {
        root.classList.remove("is-open");
        trigger.setAttribute("aria-expanded", "false");
        openPopovers.delete(popover);
        if (focus) trigger.focus();
      },
    };
    trigger.addEventListener("click", () => (root.classList.contains("is-open") ? popover.close() : popover.open()));
    return popover;
  }

  function monthHeader(month, onShift) {
    const head = el("div", "cal-head");
    const prev = el("button", "cal-nav", "‹");
    prev.type = "button";
    prev.setAttribute("aria-label", "Предыдущий месяц");
    const next = el("button", "cal-nav", "›");
    next.type = "button";
    next.setAttribute("aria-label", "Следующий месяц");
    prev.addEventListener("click", (event) => { event.stopPropagation(); onShift(-1); });
    next.addEventListener("click", (event) => { event.stopPropagation(); onShift(1); });
    head.append(prev, el("strong", "", `${MONTHS[month.getMonth()]} ${month.getFullYear()}`), next);
    return head;
  }

  function calendarTrigger(placeholder) {
    const trigger = el("button", "date-trigger");
    trigger.type = "button";
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-expanded", "false");
    trigger.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-calendar"/></svg>';
    const text = el("span", "date-text", placeholder);
    trigger.append(text, chevron());
    return { trigger, text };
  }

  function enhanceRange(root) {
    if (root.dataset.enhanced) return;
    const inputs = root.querySelectorAll('input[type="date"]');
    if (inputs.length !== 2) return;
    root.dataset.enhanced = "1";
    const [fromInput, toInput] = inputs;
    const required = fromInput.required || toInput.required;
    root.classList.add("is-enhanced");
    const { trigger, text } = calendarTrigger("Все даты");
    root.append(trigger);

    let draftStart = null;
    let draftEnd = null;
    let month = null;

    const label = () => {
      const start = parse(fromInput.value);
      const end = parse(toInput.value);
      root.classList.toggle("is-placeholder", !start && !end);
      if (start && end) return sameDay(start, end) ? human(start) : `${human(start)} — ${human(end)}`;
      if (start) return `с ${human(start)}`;
      if (end) return `по ${human(end)}`;
      return "Все даты";
    };
    const sync = () => { text.textContent = label(); };

    const apply = (start, end) => {
      fromInput.value = start ? iso(start) : "";
      toInput.value = end ? iso(end) : (start ? iso(start) : "");
      sync();
      popover.close(true);
      fire(fromInput, "input", "change");
      fire(toInput, "input", "change");
      if (root.dataset.autosubmit !== undefined) {
        const form = root.closest("form");
        if (form) (form.requestSubmit ? form.requestSubmit() : form.submit());
      }
    };

    const now = today();
    const presets = [
      ["Сегодня", now, now],
      ["Вчера", addDays(now, -1), addDays(now, -1)],
      ["Последние 7 дней", addDays(now, -6), now],
      ["Последние 10 дней", addDays(now, -9), now],
      ["Последние 30 дней", addDays(now, -29), now],
      ["Этот месяц", new Date(now.getFullYear(), now.getMonth(), 1), now],
      ["Прошлый месяц", new Date(now.getFullYear(), now.getMonth() - 1, 1), new Date(now.getFullYear(), now.getMonth(), 0)],
    ];
    if (!required) presets.push(["Все даты", null, null]);

    const render = (panel) => {
      panel.textContent = "";
      panel.classList.add("is-range");
      if (!month) month = new Date((draftStart || now).getFullYear(), (draftStart || now).getMonth(), 1);
      const side = el("div", "cal-presets");
      presets.forEach(([title, start, end]) => {
        const button = el("button", "cal-preset", title);
        button.type = "button";
        const current = parse(fromInput.value);
        const currentEnd = parse(toInput.value);
        if ((start === null && !current && !currentEnd) || (sameDay(start, current) && sameDay(end, currentEnd))) button.classList.add("is-active");
        button.addEventListener("click", () => apply(start, end));
        side.append(button);
      });
      const body = el("div", "cal-body");
      body.append(monthHeader(month, (shift) => {
        month = new Date(month.getFullYear(), month.getMonth() + shift, 1);
        render(panel);
      }));
      body.append(calendarGrid(month, {
        start: draftStart,
        end: draftEnd,
        onPick(date) {
          if (!draftStart || draftEnd) {
            draftStart = date;
            draftEnd = null;
          } else if (date < draftStart) {
            draftEnd = draftStart;
            draftStart = date;
          } else {
            draftEnd = date;
          }
          render(panel);
        },
      }));
      const foot = el("div", "cal-foot");
      const summary = el("span", "cal-summary", draftStart
        ? (draftEnd ? `${human(draftStart)} — ${human(draftEnd)}` : `${human(draftStart)} — выберите конец`)
        : "Выберите начало периода");
      const done = el("button", "btn-sm", "Применить");
      done.type = "button";
      done.disabled = !draftStart;
      done.addEventListener("click", () => apply(draftStart, draftEnd || draftStart));
      foot.append(summary, done);
      body.append(foot);
      panel.append(side, body);
    };

    const popover = makeCalendarPopover({
      root,
      trigger,
      render,
      onOpen() {
        draftStart = parse(fromInput.value);
        draftEnd = parse(toInput.value);
        month = null;
      },
    });
    draftStart = parse(fromInput.value);
    draftEnd = parse(toInput.value);
    sync();
  }

  function enhanceDate(input) {
    if (input.dataset.enhanced || input.closest("[data-daterange]") || input.dataset.native !== undefined) return;
    input.dataset.enhanced = "1";
    const root = el("div", "datepick");
    input.parentNode.insertBefore(root, input);
    root.append(input);
    input.classList.add("date-native");
    input.tabIndex = -1;
    const { trigger, text } = calendarTrigger("Выберите дату");
    root.append(trigger);
    let month = null;
    const sync = () => {
      const value = parse(input.value);
      text.textContent = value ? human(value) : "Выберите дату";
      root.classList.toggle("is-placeholder", !value);
    };
    const render = (panel) => {
      panel.textContent = "";
      const selected = parse(input.value);
      if (!month) month = new Date((selected || today()).getFullYear(), (selected || today()).getMonth(), 1);
      panel.append(monthHeader(month, (shift) => {
        month = new Date(month.getFullYear(), month.getMonth() + shift, 1);
        render(panel);
      }));
      panel.append(calendarGrid(month, {
        start: selected,
        single: true,
        onPick(date) {
          input.value = iso(date);
          sync();
          popover.close(true);
          fire(input, "input", "change");
        },
      }));
      const foot = el("div", "cal-foot");
      const todayButton = el("button", "btn-ghost btn-sm", "Сегодня");
      todayButton.type = "button";
      todayButton.addEventListener("click", () => {
        input.value = iso(today());
        sync();
        popover.close(true);
        fire(input, "input", "change");
      });
      foot.append(todayButton);
      if (!input.required) {
        const clear = el("button", "btn-ghost btn-sm", "Очистить");
        clear.type = "button";
        clear.addEventListener("click", () => {
          input.value = "";
          sync();
          popover.close(true);
          fire(input, "input", "change");
        });
        foot.append(clear);
      }
      panel.append(foot);
    };
    const popover = makeCalendarPopover({ root, trigger, render, onOpen() { month = null; } });
    input.addEventListener("change", sync);
    sync();
  }

  function enhance(scope = document) {
    scope.querySelectorAll("[data-daterange]").forEach(enhanceRange);
    scope.querySelectorAll('input[type="date"]').forEach(enhanceDate);
    scope.querySelectorAll("select").forEach(enhanceSelect);
  }
  window.nuppiEnhance = enhance;
  enhance();
  window.addEventListener("resize", () => closeAll());
})();
