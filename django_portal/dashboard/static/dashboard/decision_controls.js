(() => {
  const decisions = ["accepted", "review", "rejected"];
  const LABELS = { accepted: "Принято", review: "На проверке", rejected: "Отклонено" };

  // Статичная демо-версия (GitHub Pages) не имеет сервера: ответ собирается
  // прямо на странице, чтобы кнопки вели себя как в настоящей панели.
  async function send(form, data, demoResult) {
    if (window.NUPPI_DEMO) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      return demoResult();
    }
    const response = await fetch(form.action, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      headers: {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
    });
    const contentType = response.headers.get("content-type") || "";
    const result = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok || !result || !result.ok) throw new Error((result && result.error) || "");
    return result;
  }

  function getStatus(form) {
    let status = form.querySelector(".decision-save-status");
    if (!status) {
      status = document.createElement("small");
      status.className = "decision-save-status";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      const actions = form.querySelector(".decision-actions");
      form.insertBefore(status, actions);
    }
    return status;
  }

  function setStatus(form, message, kind) {
    const status = getStatus(form);
    status.textContent = message;
    status.className = `decision-save-status is-visible ${kind}`;
  }

  function updateCounter(decision, change) {
    const count = document.querySelector(`.stat-card.${decision} .count, .status-tab.${decision} .count`);
    if (!count) return;
    const value = Number.parseInt(count.textContent, 10);
    if (Number.isFinite(value)) count.textContent = String(Math.max(0, value + change));
    count.classList.remove("is-bumped");
    void count.offsetWidth;
    count.classList.add("is-bumped");
  }

  function updateSidebarBadge(change) {
    const badge = document.querySelector(".side-badge");
    if (!badge) return;
    const value = Math.max(0, (Number.parseInt(badge.textContent, 10) || 0) + change);
    badge.textContent = String(value);
    badge.hidden = value === 0;
  }

  function syncOtherCards(card, result) {
    // То же фото может быть и в списке, и в открытом окне — обновляем оба.
    const id = card && card.dataset.photoId;
    if (!id) return;
    document.querySelectorAll(`[data-photo-card][data-photo-id="${id}"]`).forEach((other) => {
      if (other === card) return;
      other.querySelectorAll(".decision-badge").forEach((badge) => {
        badge.classList.remove(...decisions);
        badge.classList.add(result.decision);
        badge.textContent = result.decision_label;
      });
      other.querySelectorAll(".decision-actions button").forEach((button) => {
        button.classList.toggle("is-selected", button.value === result.decision);
      });
      const reason = other.querySelector('input[name="reason"]');
      const ownReason = card.querySelector('input[name="reason"]');
      if (reason && ownReason) reason.value = ownReason.value;
    });
  }

  function updateCard(form, result) {
    const card = form.closest("[data-photo-card]");
    syncOtherCards(card, result);
    const badge = card && card.querySelector(".decision-badge");
    if (badge) {
      badge.classList.remove(...decisions, "is-updated");
      badge.classList.add(result.decision);
      badge.textContent = result.decision_label;
      void badge.offsetWidth;
      badge.classList.add("is-updated");
    }
    if (card) {
      card.classList.remove("is-saved");
      void card.offsetWidth;
      card.classList.add("is-saved");
    }

    form.querySelectorAll(".decision-actions button").forEach((button) => {
      button.classList.toggle("is-selected", button.value === result.decision);
    });


    if (result.previous_decision !== result.decision) {
      updateCounter(result.previous_decision, -1);
      updateCounter(result.decision, 1);
      const change = (result.decision === "review" ? 1 : 0) - (result.previous_decision === "review" ? 1 : 0);
      if (change) updateSidebarBadge(change);
    }
  }

  function getMetadataStatus(form) {
    let status = form.querySelector(".metadata-save-status");
    if (!status) {
      status = document.createElement("small");
      status.className = "metadata-save-status";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      form.appendChild(status);
    }
    return status;
  }

  function setMetadataStatus(form, message, kind) {
    const status = getMetadataStatus(form);
    status.textContent = message;
    status.className = `metadata-save-status is-visible ${kind}`;
  }

  function updateMetadataCard(form, result) {
    const card = form.closest("[data-photo-card]");
    if (!card) return;
    const id = card.dataset.photoId;
    const targets = id ? document.querySelectorAll(`[data-photo-card][data-photo-id="${id}"]`) : [card];
    targets.forEach((target) => {
      const date = target.querySelector(".photo-package-date span");
      const mother = target.querySelector(".photo-mother-name span");
      const qr = target.querySelector(".photo-qr-data span");
      const codeStatus = target.querySelector(".photo-code-status span");
      if (date) date.textContent = result.photo_date || "—";
      if (mother) mother.textContent = result.mother_name || "—";
      if (qr) qr.textContent = result.qr_data || "—";
      if (codeStatus) codeStatus.textContent = result.code_status || "—";
      const brand = target.querySelector(".photo-package-brand span");
      const formula = target.querySelector(".photo-package-formula span");
      if (brand && result.package_brand !== undefined) brand.textContent = result.package_brand || "—";
      if (formula && result.package_formula !== undefined) formula.textContent = result.package_formula || "—";
    });
  }

  // Готовая причина отклонения подставляется в комментарий одним нажатием.
  document.addEventListener("click", (event) => {
    const chip = event.target.closest(".reason-chip");
    if (!chip) return;
    const form = chip.closest(".decision-form");
    const input = form && form.querySelector('input[name="reason"]');
    if (!input) return;
    input.value = chip.dataset.reason;
    input.focus();
    form.querySelectorAll(".reason-chip").forEach((other) => other.classList.toggle("is-picked", other === chip));
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest(".decision-form");
    if (!form) return;

    event.preventDefault();
    const submitter = event.submitter;
    if (!submitter || !submitter.value) return;

    const reason = form.querySelector('input[name="reason"]');
    if (submitter.value === "rejected" && reason && !reason.value.trim()) {
      setStatus(form, "Напишите причину отклонения — медик увидит её в боте.", "error");
      reason.focus();
      return;
    }

    const buttons = [...form.querySelectorAll(".decision-actions button")];
    const data = new FormData(form);
    data.set("decision", submitter.value);
    buttons.forEach((button) => { button.disabled = true; });
    setStatus(form, "Сохраняю…", "success");

    try {
      const card = form.closest("[data-photo-card]");
      const result = await send(form, data, () => {
        const badge = card && card.querySelector(".decision-badge");
        const previous = decisions.find((key) => badge && badge.classList.contains(key)) || "review";
        return { ok: true, decision: submitter.value, decision_label: LABELS[submitter.value], previous_decision: previous };
      });
      updateCard(form, result);
      setStatus(form, `Сохранено: ${result.decision_label}.`, "success");
      const number = (card && card.dataset.photoId) || (form.action.match(/photo\/(\d+)\//) || [])[1];
      if (window.nuppiToast) window.nuppiToast(`Фото${number ? ` №${number}` : ""}: ${result.decision_label}`);
    } catch (error) {
      setStatus(form, error.message || "Не удалось сохранить итог.", "error");
    } finally {
      buttons.forEach((button) => { button.disabled = false; });
    }
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest(".photo-edit-form");
    if (!form) return;

    event.preventDefault();
    const button = event.submitter || form.querySelector('button[type="submit"], button:not([type])');
    if (button) button.disabled = true;
    setMetadataStatus(form, "Сохраняю…", "success");

    try {
      const result = await send(form, new FormData(form), () => {
        const card = form.closest("[data-photo-card]");
        const code = card && card.querySelector(".photo-code-status span");
        return {
          ok: true,
          photo_date: form.elements.photo_date.value,
          mother_name: form.elements.mother_name.value.trim(),
          qr_data: form.elements.qr_data.value.trim(),
          package_brand: form.elements.package_brand ? form.elements.package_brand.value.trim().toUpperCase() : undefined,
          package_formula: form.elements.package_formula ? form.elements.package_formula.value : undefined,
          code_status: code ? code.textContent : "",
          message: "Данные фотографии сохранены.",
        };
      }).catch((error) => {
        throw new Error(error.message || "Не удалось сохранить данные фотографии.");
      });
      updateMetadataCard(form, result);
      setMetadataStatus(form, result.message || "Данные фотографии сохранены.", "success");
      if (window.nuppiToast) window.nuppiToast(result.message || "Данные фотографии сохранены.");
    } catch (error) {
      setMetadataStatus(form, error.message || "Не удалось сохранить данные фотографии.", "error");
    } finally {
      if (button) button.disabled = false;
    }
  });
})();
