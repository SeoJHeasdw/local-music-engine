import { byId, h, icon } from "./dom.ts";

// ---- Toasts: short, one job each; an action names exactly what it does ----
export function toast(message: string, options: { tone?: "info" | "ok" | "error"; action?: { label: string; run: () => void }; timeout?: number } = {}): void {
  const region = byId("toasts");
  const item = h(
    "div",
    { class: `toast tone-${options.tone ?? "info"}`, role: options.tone === "error" ? "alert" : "status" },
    h("span", { class: "toast-text" }, message),
    options.action &&
      h(
        "button",
        {
          type: "button",
          class: "toast-action",
          onClick: () => {
            options.action?.run();
            item.remove();
          },
        },
        options.action.label,
      ),
    h("button", { type: "button", class: "toast-close", "aria-label": "알림 닫기", onClick: () => item.remove() }, icon("close", 14)),
  );
  region.append(item);
  while (region.children.length > 3) region.firstElementChild?.remove();
  window.setTimeout(() => item.remove(), options.timeout ?? (options.tone === "error" ? 7000 : 4200));
}

export function errorText(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  // Electron prefixes IPC errors; the person only needs the engine's sentence.
  return raw.replace(/^Error invoking remote method '[^']+': (Error: )?/, "");
}

// ---- Tooltips: data-tip on any element ----
export function installTooltips(): void {
  const tip = byId("tooltip");
  let timer = 0;
  let current: HTMLElement | null = null;
  const hide = () => {
    window.clearTimeout(timer);
    current = null;
    tip.hidden = true;
  };
  document.addEventListener("pointerover", (event) => {
    const target = (event.target as HTMLElement | null)?.closest<HTMLElement>("[data-tip]");
    if (target === current) return;
    hide();
    if (!target?.dataset.tip) return;
    current = target;
    timer = window.setTimeout(() => {
      if (!current?.isConnected || !current.dataset.tip) return;
      tip.textContent = current.dataset.tip;
      tip.hidden = false;
      const rect = current.getBoundingClientRect();
      const box = tip.getBoundingClientRect();
      const left = Math.min(window.innerWidth - box.width - 8, Math.max(8, rect.left + rect.width / 2 - box.width / 2));
      const above = rect.top - box.height - 8;
      tip.style.left = `${left}px`;
      tip.style.top = `${above > 8 ? above : rect.bottom + 8}px`;
    }, 380);
  });
  document.addEventListener("pointerdown", hide, true);
  document.addEventListener("keydown", hide, true);
  window.addEventListener("blur", hide);
}

// ---- Busy buttons keep their width and say what is happening ----
export async function withBusy<T>(button: HTMLButtonElement | null, busyLabel: string, work: () => Promise<T>): Promise<T | undefined> {
  if (button?.disabled) return undefined;
  const previous = button ? Array.from(button.childNodes) : [];
  if (button) {
    button.disabled = true;
    button.classList.add("is-busy");
    button.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), busyLabel);
  }
  try {
    return await work();
  } catch (error) {
    toast(errorText(error), { tone: "error" });
    return undefined;
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.classList.remove("is-busy");
      button.replaceChildren(...previous);
    }
  }
}

export function confirmDialog(options: { title: string; body: string; confirm: string; danger?: boolean }): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = h(
      "dialog",
      { class: "dialog confirm-dialog" },
      h(
        "form",
        { method: "dialog", class: "dialog-body" },
        h("h2", { class: "dialog-title" }, options.title),
        h("p", { class: "dialog-text" }, options.body),
        h(
          "div",
          { class: "dialog-actions" },
          h("button", { type: "submit", value: "cancel", class: "button ghost" }, "취소"),
          h("button", { type: "submit", value: "ok", class: `button ${options.danger ? "danger" : "primary"}` }, options.confirm),
        ),
      ),
    );
    document.body.append(dialog);
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "ok");
      dialog.remove();
    });
    dialog.showModal();
  });
}
