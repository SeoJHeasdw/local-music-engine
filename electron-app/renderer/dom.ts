// Minimal element builder. Text always goes through textContent, never innerHTML, so
// song titles, lyrics and prompts can never become markup.

export type Child = Node | string | number | null | undefined | false | Child[];
type Handler = (event: any) => void;
export type Props = Record<string, unknown> & {
  class?: string | false | null;
  dataset?: Record<string, string>;
  style?: Record<string, string>;
};

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props?: Props | null,
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") element.className = String(value);
      else if (key === "dataset") Object.assign(element.dataset, value);
      else if (key === "style") Object.assign(element.style, value);
      else if (key === "value") (element as unknown as { value: string }).value = String(value);
      else if (key.startsWith("on") && typeof value === "function") {
        element.addEventListener(key.slice(2).toLowerCase(), value as Handler);
      } else if (key in element && typeof value !== "string") {
        (element as unknown as Record<string, unknown>)[key] = value;
      } else if (value === true) element.setAttribute(key, "");
      else element.setAttribute(key, String(value));
    }
  }
  append(element, children);
  return element;
}

export function append(parent: Node, children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(parent, child);
    else parent.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function mount(parent: Element, ...children: Child[]): void {
  parent.replaceChildren();
  append(parent, children);
}

export function byId<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`missing element: ${id}`);
  return element as T;
}

// Static, trusted icon paths (24px grid, stroked).
const ICONS: Record<string, string> = {
  library: '<path d="M4 5.5h5v13H4zM10.5 5.5h4v13h-4z"/><path d="m16.2 6.2 3.6-.9 3 12.3-3.6.9z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  studio: '<path d="M3 12h2.5M6.5 8v8M10 5v14M13.5 9v6M17 7v10M20.5 11v2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke="none"/>',
  pause: '<path d="M7.5 5.5h3v13h-3zM13.5 5.5h3v13h-3z" fill="currentColor" stroke="none"/>',
  back5: '<path d="M4 12a8 8 0 1 0 2.4-5.7"/><path d="M4 4v4h4"/><text x="12" y="15.5" font-size="7.5" text-anchor="middle" fill="currentColor" stroke="none" font-family="system-ui" font-weight="700">5</text>',
  fwd5: '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4v4h-4"/><text x="12" y="15.5" font-size="7.5" text-anchor="middle" fill="currentColor" stroke="none" font-family="system-ui" font-weight="700">5</text>',
  start: '<path d="M6 5v14"/><path d="M18 5.5v13L9 12z" fill="currentColor" stroke="none"/>',
  loop: '<path d="M17 2.5 20.5 6 17 9.5"/><path d="M3.5 11V9.5A3.5 3.5 0 0 1 7 6h13.5"/><path d="M7 21.5 3.5 18 7 14.5"/><path d="M20.5 13v1.5A3.5 3.5 0 0 1 17 18H3.5"/>',
  folder: '<path d="M3.5 7.5A2 2 0 0 1 5.5 5.5h3.6l2 2.2h7.4a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
  export: '<path d="M12 3.5v11"/><path d="m7.5 10 4.5 4.5 4.5-4.5"/><path d="M4.5 16v2.5a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V16"/>',
  sparkle: '<path d="M12 3.5c.6 4.2 2.3 5.9 6.5 6.5-4.2.6-5.9 2.3-6.5 6.5-.6-4.2-2.3-5.9-6.5-6.5 4.2-.6 5.9-2.3 6.5-6.5Z"/><path d="M18.5 15v4.5M16.25 17.25h4.5"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  thumbUp: '<path d="M7.5 10.5v9h-3v-9z"/><path d="M7.5 10.5 11 3.8a1.9 1.9 0 0 1 3.4 1.5l-.9 3.7h5a2 2 0 0 1 2 2.4l-1.3 6.2a2 2 0 0 1-2 1.6H7.5"/>',
  thumbDown: '<path d="M7.5 13.5v-9h-3v9z"/><path d="M7.5 13.5 11 20.2a1.9 1.9 0 0 0 3.4-1.5l-.9-3.7h5a2 2 0 0 0 2-2.4l-1.3-6.2a2 2 0 0 0-2-1.6H7.5"/>',
  meh: '<circle cx="12" cy="12" r="8.5"/><path d="M8.5 15h7"/><circle cx="9" cy="10" r=".6" fill="currentColor"/><circle cx="15" cy="10" r=".6" fill="currentColor"/>',
  star: '<path d="m12 3.8 2.5 5.2 5.7.8-4.1 4 1 5.6L12 16.8l-5.1 2.6 1-5.6-4.1-4 5.7-.8z"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5v-2a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>',
  wand: '<path d="m4 20 11-11"/><path d="m13.5 5.5 1-2.5 1 2.5 2.5 1-2.5 1-1 2.5-1-2.5-2.5-1z"/><path d="M19 13.5v3M17.5 15h3"/>',
  refresh: '<path d="M20 11.5A8 8 0 0 0 6.3 6.3L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M4 12.5a8 8 0 0 0 13.7 5.2l2.3-2.2"/><path d="M20 20v-4.5h-4.5"/>',
  power: '<path d="M12 3.5v8"/><path d="M6.6 6.8a7.5 7.5 0 1 0 10.8 0"/>',
  undo: '<path d="M8 5 3.5 9.5 8 14"/><path d="M3.5 9.5H15a5.5 5.5 0 0 1 0 11h-3"/>',
  branch: '<path d="M6 3.5v11a4 4 0 0 0 4 4h8"/><path d="m15 15.5 3 3-3 3"/>',
  sidebar: '<rect x="3.25" y="4.25" width="17.5" height="15.5" rx="3.5"/><path d="M9.25 4.25v15.5"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.6v.1"/>',
  lyrics: '<path d="M5 6h14M5 10.5h14M5 15h9M5 19.5h6"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  volume: '<path d="M4.5 9.5v5h3.5l5 4v-13l-5 4z"/><path d="M16 9a4.5 4.5 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/>',
  compare: '<path d="M8 4.5v15M16 4.5v15"/><path d="M3.5 9 8 4.5 12.5 9"/><path d="M11.5 15 16 19.5l4.5-4.5"/>',
};

export function icon(name: keyof typeof ICONS | string, size = 18): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] ?? "";
  return svg;
}

export function isTyping(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element) return false;
  return element.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName);
}
