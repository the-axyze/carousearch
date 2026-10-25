// Requests, debouncing and swaps are HTMX attributes in the templates. This file adds the "/" and Esc keys,
// the Details toggle and Close button, and the form state that HTMX's history snapshots need.
(() => {
  document.addEventListener("keydown", (event) => {
    const inField = event.target.closest("input, select, textarea");
    if (event.key === "/" && !inField) {
      event.preventDefault();
      document.getElementById("query").focus();
    } else if (event.key === "Escape" && inField) event.target.blur();
  });
  const closeDetail = (button) => {
    button.setAttribute("aria-expanded", "false");
    document.getElementById(button.getAttribute("aria-controls")).replaceChildren();
  };
  // A Details button whose panel is open closes it instead of loading the listing again.
  document.addEventListener("htmx:beforeRequest", (event) => {
    if (event.detail.elt.matches("[data-detail-toggle][aria-expanded='true']")) {
      event.preventDefault();
      closeDetail(event.detail.elt);
    }
  });
  document.addEventListener("click", (event) => {
    const panel = event.target.closest("[data-detail-close]")?.closest(".detail");
    if (panel) closeDetail(document.querySelector(`[aria-controls="${panel.id}"]`));
  });

  // HTMX saves the page's HTML before each search, and live form values are not in the HTML. Writing them in
  // once results arrive makes Back and Forward show the filters that produced each page of results.
  document.addEventListener("htmx:afterSwap", (event) => {
    const trigger = event.detail.requestConfig?.elt;
    if (trigger?.matches("[data-detail-toggle]")) trigger.setAttribute("aria-expanded", "true");
    if (event.detail.target?.id !== "results") return;
    for (const field of document.querySelectorAll("#search-form input")) {
      if (field.type === "checkbox") field.toggleAttribute("checked", field.checked);
      else field.setAttribute("value", field.value);
    }
    for (const option of document.querySelectorAll("#search-form option")) option.toggleAttribute("selected", option.selected);
  });
})();
