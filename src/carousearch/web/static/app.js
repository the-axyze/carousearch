// Requests, debouncing and swaps are HTMX attributes in the templates. This file adds the "/", Esc and Ctrl+Enter
// keys, the Filters | Ask switch's address, Ask's method templates, Ask's refusal messages, the Details toggle and
// Close button, the form state that HTMX's history snapshots need, and outreach's Send button count.
(() => {
  const askMode = () => document.getElementById("mode-ask")?.checked;
  document.addEventListener("keydown", (event) => {
    const inField = event.target.closest("input, select, textarea");
    if (event.key === "/" && !inField) {
      event.preventDefault();
      document.getElementById(askMode() ? "prompt" : "query").focus();
    } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && event.target.id === "prompt") {
      event.preventDefault();
      if (!document.getElementById("ask-run-button").disabled) document.getElementById("ask-form").requestSubmit();
    } else if (event.key === "Escape" && inField) event.target.blur();
  });

  // The switch only shows one section or the other (CSS). The address follows it, so a reload keeps the mode;
  // the Filters address (with its search) comes back when switching back.
  let filtersAddress = askMode() ? "/" : location.pathname + location.search;
  document.addEventListener("change", (event) => {
    if (event.target.name !== "mode" || !event.target.closest(".mode-switch")) return;
    for (const radio of document.querySelectorAll(".mode-switch input")) radio.toggleAttribute("checked", radio.checked);
    if (askMode()) filtersAddress = location.pathname + location.search;
    history.replaceState(history.state, "", askMode() ? "/?mode=ask" : filtersAddress);
    document.getElementById(askMode() ? "prompt" : "query").focus();
  });

  // A method template puts its example request, with <placeholders> to replace, in the prompt box; nothing runs.
  document.addEventListener("click", (event) => {
    const template = event.target.closest("[data-prefill]");
    if (!template) return;
    const prompt = document.getElementById("prompt");
    prompt.value = template.dataset.prefill;
    prompt.focus();
    const placeholder = prompt.value.indexOf("<");
    if (placeholder >= 0) prompt.setSelectionRange(placeholder, prompt.value.indexOf(">", placeholder) + 1);
  });

  // Ask's refusals (no key, empty prompt, a run in progress, an unknown run) come with a 4xx status, which HTMX
  // does not swap by default; in the Ask section, show them.
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.elt.closest(".ask-mode") && [403, 404, 409, 422].includes(event.detail.xhr.status)) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
      // A refused send keeps the drafts on the page and shows its reason above the Send button.
      if (event.detail.elt.id === "outreach-review") {
        event.detail.target = document.getElementById("outreach-review-error");
        event.detail.swapOverride = "innerHTML";
      }
    }
  });
  // Outreach: the Send button says how many drafts are ticked, and is off when none are.
  document.addEventListener("change", (event) => {
    if (!event.target.matches("[data-outreach-check]")) return;
    const form = event.target.closest("form");
    const count = form.querySelectorAll("[data-outreach-check]:checked").length;
    const button = form.querySelector("[data-outreach-send]");
    button.textContent = `Send ${count} message${count === 1 ? "" : "s"}`;
    button.disabled = count === 0;
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
