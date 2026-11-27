(() => {
  const decisions = ["accepted", "review", "rejected"];

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
    const count = document.querySelector(`.decision-card.${decision} b`);
    if (!count) return;
    const value = Number.parseInt(count.textContent, 10);
    if (Number.isFinite(value)) count.textContent = String(Math.max(0, value + change));
  }

  function updateCard(form, result) {
    const card = form.closest(".card");
    const badge = card && card.querySelector(".decision-badge");
    if (badge) {
      badge.classList.remove(...decisions);
      badge.classList.add(result.decision);
      badge.textContent = result.decision_label;
    }

    form.querySelectorAll(".decision-actions button").forEach((button) => {
      button.classList.toggle("is-selected", button.value === result.decision);
    });

    let current = form.querySelector(".manual-decision-current");
    if (!current) {
      current = document.createElement("small");
      current.className = "manual-decision-current";
      form.insertBefore(current, getStatus(form));
    }
    current.textContent = `Сейчас установлено оператором: ${result.decision_label}`;

    if (result.previous_decision !== result.decision) {
      updateCounter(result.previous_decision, -1);
      updateCounter(result.decision, 1);
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
    const card = form.closest(".card");
    if (!card) return;
    const date = card.querySelector(".photo-package-date span");
    const mother = card.querySelector(".photo-mother-name span");
    const qr = card.querySelector(".photo-qr-data span");
    const codeStatus = card.querySelector(".photo-code-status span");
    if (date) date.textContent = result.photo_date || "—";
    if (mother) mother.textContent = result.mother_name || "—";
    if (qr) qr.textContent = result.qr_data || "—";
    if (codeStatus) codeStatus.textContent = result.code_status || "—";
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest(".decision-form");
    if (!form) return;

    event.preventDefault();
    const submitter = event.submitter;
    if (!submitter || !submitter.value) return;

    const buttons = [...form.querySelectorAll(".decision-actions button")];
    const data = new FormData(form);
    data.set("decision", submitter.value);
    buttons.forEach((button) => { button.disabled = true; });
    setStatus(form, "Сохраняю…", "success");

    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: data,
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "Не удалось сохранить итог.");
      updateCard(form, result);
      setStatus(form, `Сохранено: ${result.decision_label}.`, "success");
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
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
      });
      const contentType = response.headers.get("content-type") || "";
      const result = contentType.includes("application/json") ? await response.json() : null;
      if (!response.ok || !result || !result.ok) {
        throw new Error((result && result.error) || "Не удалось сохранить данные фотографии.");
      }
      updateMetadataCard(form, result);
      setMetadataStatus(form, result.message || "Данные фотографии сохранены.", "success");
    } catch (error) {
      setMetadataStatus(form, error.message || "Не удалось сохранить данные фотографии.", "error");
    } finally {
      if (button) button.disabled = false;
    }
  });
})();
