// A media type's list controls, kept in the fields of the form with x-ref="form", which reloads
// the results on each change. The sort and layout are its hidden fields, the filters are the
// inputs of the panel with x-ref="panel" (named by their URL params). The sorts come from the
// JSON in the element with id="library-sorts", each with its usual direction and how either
// direction reads.
// Guard against re-registering when this script re-runs after an HTMX body swap.
if (!window.__libraryFiltersRegistered) {
  window.__libraryFiltersRegistered = true;
  document.addEventListener("alpine:init", () => {
    Alpine.data("libraryFilters", (state) => ({
      ...state,
      panelOpen: false,
      sorts: JSON.parse(document.getElementById("library-sorts").textContent),
      // The results, which replace themselves, say how many there are and how many filters apply
      syncResults() {
        const results = document.getElementById("library-results");
        if (results?.dataset.count) {
          this.count = Number(results.dataset.count);
          this.filterCount = Number(results.dataset.filterCount);
        }
        // New results behind the open panel stay out of reach too
        if (this.panelOpen) this.makePageInert();
      },
      // The selected sort's name and how its direction reads
      get sortLabel() {
        return this.sortChoice().label;
      },
      // The selected sort's two direction names, ascending first
      get orderLabels() {
        return this.sortChoice().order_labels;
      },
      // The name of the current direction of the selected sort
      get orderLabel() {
        return this.orderLabels[this.order === "asc" ? 0 : 1];
      },
      // What the panel's button offers to show, as the media type's name
      get showLabel() {
        return `Show ${this.count.toLocaleString()} ${this.count === 1 ? this.noun : this.nounPlural}`;
      },
      sortChoice(value = this.sort) {
        return this.sorts.find((choice) => choice.value === value) || this.sorts[0];
      },
      // Sorting by the selected sort again flips it, a new one starts in its usual direction
      sortBy(value) {
        if (value === this.sort) {
          this.order = this.order === "asc" ? "desc" : "asc";
        } else {
          this.sort = value;
          this.order = this.sortChoice(value).order;
        }
        this.submit();
      },
      set(field, value) {
        this[field] = value;
        this.submit();
      },
      // While the panel is open the rest of the page is inert and doesn't scroll
      openPanel() {
        this.panelOpen = true;
        document.body.classList.add("overflow-hidden");
        this.inerted = [];
        this.makePageInert();
        this.$nextTick(() => this.$refs.panel.focus());
      },
      // Makes everything outside the panel inert, remembering what it changed to undo on close
      makePageInert() {
        for (let node = this.$refs.dialog; node && node !== document.body; node = node.parentElement) {
          for (const sibling of node.parentElement.children) {
            if (sibling === node || sibling.inert || sibling.tagName === "SCRIPT") continue;
            sibling.inert = true;
            this.inerted.push(sibling);
          }
        }
      },
      // Closes the panel, undoing what openPanel did and returning focus to its button
      closePanel() {
        if (!this.panelOpen) return;
        this.panelOpen = false;
        document.body.classList.remove("overflow-hidden");
        for (const element of this.inerted) element.inert = false;
        this.inerted = [];
        this.$refs.panelButton.focus();
      },
      // Empties the inputs named by these params, or with a `value` only that choice's checkbox
      removeFilter(params, value) {
        for (const input of this.$refs.panel.querySelectorAll("input")) {
          if (!params.includes(input.name)) continue;
          if (input.type !== "checkbox") input.value = "";
          else if (!value || input.value.toLowerCase() === value.toLowerCase()) input.checked = false;
        }
        this.submit();
      },
      // Clears the filters, and the search and status too with `everything`
      clearFilters(everything = false) {
        for (const input of this.$refs.panel.querySelectorAll("input")) {
          if (input.type === "checkbox") input.checked = false;
          else input.value = "";
        }
        if (everything) Object.assign(this, { search: "", status: "All" });
        this.submit();
      },
      // Waits for the fields to take the new values before sending them
      submit() {
        this.$nextTick(() => htmx.trigger(this.$refs.form, "submit"));
      },
    }));
  });
}
