/** Local workspace navigation. Panels stay mounted so polling never loses input. */
export function mountWorkspace() {
  const tabs = [...document.querySelectorAll('[role="tab"][data-view]')];
  function activate(tab, focus = false) {
    tabs.forEach(item => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      document.getElementById(item.getAttribute("aria-controls")).hidden = !selected;
    });
    if (focus) tab.focus();
  }
  try {
    const next = localStorage.getItem("cd_console_next_view");
    localStorage.removeItem("cd_console_next_view");
    const tab = tabs.find(item => item.dataset.view === next);
    if (tab) activate(tab);
  } catch {}
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activate(tab));
    tab.addEventListener("keydown", event => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        activate(tabs[next], true);
      }
    });
  });
}
