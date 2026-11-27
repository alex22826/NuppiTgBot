document.addEventListener("DOMContentLoaded", () => {
    const dialog = document.getElementById("photo-viewer");
    const image = document.getElementById("photo-viewer-image");
    const angleOutput = document.getElementById("photo-viewer-angle");
    const openLink = document.getElementById("photo-viewer-open");
    const downloadLink = document.getElementById("photo-viewer-download");

    if (!dialog || !image || typeof dialog.showModal !== "function") {
        return;
    }

    let angle = 0;
    let zoom = 1;
    let panX = 0;
    let panY = 0;
    let dragStartX = 0;
    let dragStartY = 0;
    let dragStartPanX = 0;
    let dragStartPanY = 0;
    let isDragging = false;
    const minZoom = 0.5;
    const maxZoom = 4;
    const zoomStep = 0.15;

    const applyTransform = () => {
        image.style.transform = `translate(${panX}px, ${panY}px) rotate(${angle}deg) scale(${zoom})`;
        angleOutput.value = `${((angle % 360) + 360) % 360}°`;
    };

    const resetTransform = () => {
        angle = 0;
        zoom = 1;
        panX = 0;
        panY = 0;
        isDragging = false;
        image.classList.remove("is-dragging");
        applyTransform();
    };

    document.querySelectorAll(".photo-viewer-link").forEach((link) => {
        link.addEventListener("click", (event) => {
            if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
                return;
            }
            event.preventDefault();
            resetTransform();
            image.src = link.href;
            image.alt = link.querySelector("img")?.alt || "Просматриваемая фотография";
            openLink.href = link.href;
            downloadLink.href = link.dataset.downloadUrl || link.href;
            dialog.showModal();
        });
    });

    dialog.querySelectorAll("[data-photo-rotate]").forEach((button) => {
        button.addEventListener("click", () => {
            angle += Number(button.dataset.photoRotate || 0);
            applyTransform();
        });
    });

    image.addEventListener("wheel", (event) => {
        event.preventDefault();
        const direction = event.deltaY < 0 ? 1 : -1;
        zoom = Math.min(maxZoom, Math.max(minZoom, zoom + direction * zoomStep));
        applyTransform();
    }, { passive: false });

    image.draggable = false;
    image.addEventListener("dragstart", (event) => event.preventDefault());
    image.addEventListener("pointerdown", (event) => {
        if (!event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) {
            return;
        }
        event.preventDefault();
        isDragging = true;
        dragStartX = event.clientX;
        dragStartY = event.clientY;
        dragStartPanX = panX;
        dragStartPanY = panY;
        image.classList.add("is-dragging");
        image.setPointerCapture(event.pointerId);
    });
    image.addEventListener("pointermove", (event) => {
        if (!isDragging || !event.isPrimary) {
            return;
        }
        event.preventDefault();
        panX = dragStartPanX + event.clientX - dragStartX;
        panY = dragStartPanY + event.clientY - dragStartY;
        applyTransform();
    });

    const finishDragging = (event) => {
        if (!isDragging) {
            return;
        }
        isDragging = false;
        image.classList.remove("is-dragging");
        if (image.hasPointerCapture(event.pointerId)) {
            image.releasePointerCapture(event.pointerId);
        }
    };
    image.addEventListener("pointerup", finishDragging);
    image.addEventListener("pointercancel", finishDragging);

    dialog.querySelector("[data-photo-reset]")?.addEventListener("click", resetTransform);
    dialog.querySelector("[data-photo-viewer-close]")?.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
        image.removeAttribute("src");
        resetTransform();
    });
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog) {
            dialog.close();
        }
    });
});
