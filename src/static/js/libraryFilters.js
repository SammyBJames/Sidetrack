// A media type's list controls, kept in the fields of the form with x-ref="form", which reloads
// the results on each change. The sort and layout are its hidden fields, the filters are the
// inputs of the panel with x-ref="panel" (named by their URL params). The sorts come from the
// JSON in the element with id="library-sorts", each with its usual direction and how either
// direction reads. The panel's slider, date and player controls keep their own state, which their
// elements with data-control-params (the params they set) can reset().
// Guard against re-registering when this script re-runs after an HTMX body swap.
if (!window.__libraryFiltersRegistered) {
  window.__libraryFiltersRegistered = true;
  document.addEventListener("alpine:init", () => {
    Alpine.data("libraryFilters", (state) => ({
      ...state,
      panelOpen: false,
      sortOpen: false,
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
      // The sorts under their headings, in the order the headings first appear
      get sortGroups() {
        const groups = [];
        for (const choice of this.sorts) {
          let group = groups.find((found) => found.title === choice.group);
          if (!group) groups.push((group = { title: choice.group, choices: [] }));
          group.choices.push(choice);
        }
        return groups;
      },
      // A sort's choice by its value, the selected sort's without one
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
      // Opens the sort menu, focusing the selected sort, or the last one with `last`
      openSort(last = false) {
        this.sortOpen = true;
        this.$nextTick(() => {
          const items = this.sortItems();
          const selected = items.find((item) => item.getAttribute("aria-checked") === "true");
          (last ? items.at(-1) : selected || items[0])?.focus();
        });
      },
      // Closes the sort menu, returning focus to its button with `refocus`
      closeSort(refocus = false) {
        if (!this.sortOpen) return;
        this.sortOpen = false;
        if (refocus) this.$refs.sortButton.focus();
      },
      // The menu's sorts as buttons, in order
      sortItems() {
        return [...this.$refs.sortMenu.querySelectorAll("[role=menuitemradio]")];
      },
      // Arrow keys, Home and End move between the menu's sorts and Tab closes it (Escape is handled at the window)
      moveSort(event) {
        const items = this.sortItems();
        const at = items.indexOf(document.activeElement);
        const target = {
          ArrowDown: items[(at + 1) % items.length],
          ArrowUp: items[(at - 1 + items.length) % items.length],
          Home: items[0],
          End: items.at(-1),
        }[event.key];
        if (target) {
          event.preventDefault();
          target.focus();
        } else if (event.key === "Tab") {
          this.closeSort();
        }
      },
      // Applies a sort chosen from the menu, which keeps the order when it is already selected, and closes the menu
      chooseSort(value) {
        if (value !== this.sort) this.sortBy(value);
        this.closeSort(true);
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
      // The classes of a chip, on or off
      chip(on) {
        return on
          ? "bg-indigo-600 text-white ring-indigo-400"
          : "bg-white/5 text-gray-300 ring-white/10 hover:bg-white/10";
      },
      // Resets the panel's controls that set any of these params, or all of them without
      resetControls(params) {
        for (const control of this.$refs.panel.querySelectorAll("[data-control-params]")) {
          const own = control.dataset.controlParams.split(",");
          if (!params || own.some((param) => params.includes(param))) Alpine.$data(control).reset();
        }
      },
      // Empties the inputs named by these params, or with a `value` only that choice's checkbox
      removeFilter(params, value) {
        this.resetControls(params);
        for (const input of this.$refs.panel.querySelectorAll("input")) {
          if (!params.includes(input.name) || input.closest("[data-control-params]")) continue;
          if (input.type !== "checkbox") input.value = "";
          else if (!value || input.value.toLowerCase() === value.toLowerCase()) input.checked = false;
        }
        this.submit();
      },
      // Clears the filters, and the search and status too with `everything`
      clearFilters(everything = false) {
        this.resetControls();
        for (const input of this.$refs.panel.querySelectorAll("input")) {
          if (!input.name || input.closest("[data-control-params]")) continue;
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

    // A two-handle range slider of two range inputs over one track, set from its data attributes:
    // the scale (floor, ceiling), the values chosen (min, max), their format and unit. A handle
    // at the end of the scale sets no filter.
    Alpine.data("rangeSlider", () => ({
      floor: 0,
      ceiling: 1,
      lo: 0,
      hi: 1,
      numberFormat: "count",
      unit: "",
      exact: false,
      // Whether a bound the address put at an end of the scale is still kept, until it's moved
      keepLo: false,
      keepHi: false,
      // Reads the scale and chosen values from the element's data attributes
      init() {
        const data = this.$el.dataset;
        this.floor = Number(data.floor);
        this.ceiling = Number(data.ceiling);
        this.lo = Number(data.min);
        this.hi = Number(data.max);
        this.numberFormat = data.format;
        this.unit = data.unit;
        this.keepLo = "keepMin" in data;
        this.keepHi = "keepMax" in data;
      },
      // Whether either handle is off the end of the scale, so a filter is set
      get active() {
        return this.lo > this.floor || this.hi < this.ceiling;
      },
      // The chosen range as words, e.g. "1h 30m – 2h 45m" or "100 – 400 pages"
      get label() {
        const suffix = this.unit === "%" ? "%" : this.numberFormat === "count" && this.unit ? ` ${this.unit}` : "";
        return `${this.format(this.lo)} \u2013 ${this.format(this.hi)}${suffix}`;
      },
      // A value as the field shows it
      format(value) {
        if (this.numberFormat === "minutes") {
          const hours = Math.floor(value / 60);
          const minutes = value % 60;
          return hours ? `${hours}h${minutes ? ` ${minutes}m` : ""}` : `${minutes}m`;
        }
        if (this.numberFormat === "year") return String(value);
        if (this.numberFormat === "decimal" && !Number.isInteger(value)) return value.toFixed(1);
        return value.toLocaleString();
      },
      // Where a value sits along the scale, 0 to 100
      percent(value) {
        return ((value - this.floor) / (this.ceiling - this.floor)) * 100;
      },
      // Takes an input's value for the lower or upper end, kept within the scale and the other end,
      // and puts back what was kept
      setBound(side, input) {
        const value = Number(input.value);
        if (input.value !== "" && !Number.isNaN(value)) {
          this[side] =
            side === "lo"
              ? Math.min(Math.max(value, this.floor), this.hi)
              : Math.max(Math.min(value, this.ceiling), this.lo);
        }
        if (side === "lo") this.keepLo = false;
        else this.keepHi = false;
        input.value = this[side];
      },
      // Puts both handles back at the ends of the scale, setting no filter
      reset() {
        this.lo = this.floor;
        this.hi = this.ceiling;
        this.keepLo = false;
        this.keepHi = false;
      },
    }));

    // A date field's presets ("" for any, "custom" for its date pickers), by the mode chosen. A
    // preset or custom dates are set, never both.
    Alpine.data("datePresets", (mode) => ({
      mode,
      // The preset to send, "" for any or custom dates
      get preset() {
        return this.mode === "custom" ? "" : this.mode;
      },
      // Picks a mode, dropping any typed dates unless it is "custom"
      choose(mode) {
        this.mode = mode;
        if (mode !== "custom") this.clearDates();
      },
      // Empties the date pickers
      clearDates() {
        for (const input of this.$el.querySelectorAll("input[type=date]")) input.value = "";
      },
      // Back to any date
      reset() {
        this.mode = "";
        this.clearDates();
      },
    }));

    // Chips for how many players a game fits, 1 to 6+, from the player counts in the address
    Alpine.data("playerChips", (min, max) => ({
      min,
      max,
      // Whether a chip is the chosen count; 6 stands for 6 or more, with no upper end
      picked(count) {
        return this.min === String(count) && this.max === (count === 6 ? "" : String(count));
      },
      // Chooses a count, or clears it if it is already chosen
      pick(count) {
        if (this.picked(count)) return this.reset();
        this.min = String(count);
        this.max = count === 6 ? "" : String(count);
      },
      // Clears the chosen count
      reset() {
        this.min = "";
        this.max = "";
      },
    }));
  });
}
