// Drag a panel edge to change its width. Double-click restores the default; with focus,
// ←/→ adjust it. The chosen width is remembered and only clamped to the window as shown.
// Same behaviour as Assets Studio's sidebar resizer.
const KEY_STEP = 16;

function remembered(key: string): number | null {
  try {
    return Number(localStorage.getItem(key)) || null;
  } catch {
    return null;
  }
}

function remember(key: string, value: number): void {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // resizing still works when storage is blocked
  }
}

export type ResizerOptions = {
  storageKey: string;
  min: number;
  max: number | (() => number);
  fallback: number;
  apply: (width: number) => void;
};

export function installResizer(handle: HTMLElement, options: ResizerOptions): void {
  const { storageKey, min, max, fallback, apply } = options;
  let preferred = remembered(storageKey) || fallback;
  let drag: { x: number; width: number } | null = null;
  const limit = (value: number) => Math.round(Math.min(Math.max(min, typeof max === "function" ? max() : max), Math.max(min, value)));

  const show = () => {
    const width = limit(preferred);
    handle.setAttribute("aria-valuenow", String(width));
    apply(width);
    return width;
  };
  const choose = (value: number) => {
    preferred = limit(value);
    remember(storageKey, show());
  };

  handle.setAttribute("role", "separator");
  handle.setAttribute("aria-orientation", "vertical");
  handle.setAttribute("aria-valuemin", String(min));
  handle.tabIndex = 0;
  show();

  // The handle is a few pixels wide; listen on the window while dragging so a fast
  // pointer never escapes it.
  const move = (event: PointerEvent) => {
    if (!drag) return;
    if (event.buttons === 0) {
      end();
      return;
    }
    preferred = limit(drag.width + event.clientX - drag.x);
    show();
  };
  const end = (event?: PointerEvent) => {
    if (!drag) return;
    // Settle on where the button was released; the last move can lag behind a fast drag.
    if (event?.type === "pointerup") preferred = limit(drag.width + event.clientX - drag.x);
    show();
    drag = null;
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", end);
    window.removeEventListener("pointercancel", end);
    handle.classList.remove("is-dragging");
    document.body.classList.remove("is-resizing");
    remember(storageKey, preferred);
  };
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    drag = { x: event.clientX, width: limit(preferred) };
    try {
      handle.setPointerCapture(event.pointerId);
    } catch {
      // window listeners are enough
    }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    handle.classList.add("is-dragging");
    document.body.classList.add("is-resizing");
  });
  handle.addEventListener("dblclick", () => choose(fallback));
  handle.addEventListener("keydown", (event) => {
    const delta = ({ ArrowLeft: -KEY_STEP, ArrowRight: KEY_STEP } as Record<string, number>)[event.key];
    if (!delta) return;
    event.preventDefault();
    choose(limit(preferred) + delta);
  });
  window.addEventListener("resize", show);
}
