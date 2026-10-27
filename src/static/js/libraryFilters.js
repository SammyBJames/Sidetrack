// A media type's list controls, kept in the fields of the form with x-ref="form", which reloads
// the results on each change. The sort and layout are its hidden fields, the filters are the
// inputs of the panel with x-ref="panel" (named by their URL params). The sorts come from the
// JSON in the element with id="library-sorts", each with its usual direction and how either
// direction reads. The panel's slider, date, player and choice rows keep their own state (whether
// they are open, and the value they summarize), which their elements with data-control-params (the
// params they set) can reset(). Their summaries repeat the words of the server's (library_fields.py).
// Guard against re-registering when this script re-runs after an HTMX body swap.
if (!window.__libraryFiltersRegistered) {
  window.__libraryFiltersRegistered = true;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // The date presets' names, as the server has them
  const PRESET_NAMES = {
    "7d": "Past week",
    "30d": "Past month",
    "90d": "Past 3 months",
    "365d": "Past year",
    year: "This year",
  };
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
      // Resets the panel's controls that set any of these params, or all of them without; with a
      // `value`, a choice row drops only that one
      resetControls(params, value) {
        for (const control of this.$refs.panel.querySelectorAll("[data-control-params]")) {
          const own = control.dataset.controlParams.split(",");
          if (!params || own.some((param) => params.includes(param))) Alpine.$data(control).reset(value);
        }
      },
      // Empties the controls that set these params, or with a `value` only that choice's checkbox
      removeFilter(params, value) {
        this.resetControls(params, value);
        this.submit();
      },
      // Clears the filters, and the search and status too with `everything`
      clearFilters(everything = false) {
        this.resetControls();
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
    // at the end of the scale sets no filter. Its row starts `open`, and may have an Unrated choice.
    Alpine.data("rangeSlider", (open) => ({
      open,
      unrated: false,
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
        this.unrated = "unrated" in data;
      },
      // Whether the lower bound is set: its handle is off the end of the scale, or it is kept
      get minSet() {
        return this.lo > this.floor || this.keepLo;
      },
      // Whether the upper bound is set, as for minSet
      get maxSet() {
        return this.hi < this.ceiling || this.keepHi;
      },
      // Whether a filter is set
      get active() {
        return this.summary !== "Any";
      },
      // What the row says: the chosen range as words, e.g. "7–10", "1h 30m+" or "up to 400 pages",
      // "Unrated" or "7–10 or unrated" with that choice, "Any" for none
      get summary() {
        const suffix = this.unit === "%" ? "%" : this.numberFormat === "count" && this.unit ? ` ${this.unit}` : "";
        const lo = this.format(this.lo);
        const hi = this.format(this.hi);
        let text = "";
        if (this.minSet && this.maxSet) text = (lo === hi ? lo : `${lo}\u2013${hi}`) + suffix;
        else if (this.minSet) text = suffix.startsWith(" ") ? `${lo}+${suffix}` : `${lo}${suffix}+`;
        else if (this.maxSet) text = `up to ${hi}${suffix}`;
        if (this.unrated) return text ? `${text} or unrated` : "Unrated";
        return text || "Any";
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
      // Puts both handles back at the ends of the scale and clears Unrated, setting no filter
      reset() {
        this.lo = this.floor;
        this.hi = this.ceiling;
        this.keepLo = false;
        this.keepHi = false;
        this.unrated = false;
      },
    }));

    // A date field's presets ("" for any, "custom" for its date pickers), by the mode chosen, and
    // the custom dates (ISO, "" for none) as `from` and `to`. A preset or custom dates are set,
    // never both. Its row starts `open`.
    Alpine.data("datePresets", (open, mode, from, to) => ({
      open,
      mode,
      from,
      to,
      // The preset to send, "" for any or custom dates
      get preset() {
        return this.mode === "custom" ? "" : this.mode;
      },
      // A preset's name, or the custom dates as words, e.g. "Mar 1 – Apr 2, 2026"; "Any" for none
      get summary() {
        if (this.mode && this.mode !== "custom") return PRESET_NAMES[this.mode] ?? this.mode;
        const start = this.mode === "custom" && this.from ? this.words(this.from) : null;
        const end = this.mode === "custom" && this.to ? this.words(this.to) : null;
        if (start && end) {
          const first = start.year === end.year ? start.day : `${start.day}, ${start.year}`;
          return `${first} \u2013 ${end.day}, ${end.year}`;
        }
        if (start) return `From ${start.day}, ${start.year}`;
        if (end) return `Until ${end.day}, ${end.year}`;
        return "Any";
      },
      // Whether a preset or any date is set
      get active() {
        return this.summary !== "Any";
      },
      // An ISO date as its year and its month and day, e.g. 2026 and "Mar 1"
      words(iso) {
        const [year, month, day] = iso.split("-").map(Number);
        return { year, day: `${MONTHS[month - 1]} ${day}` };
      },
      // Picks a mode, dropping any typed dates unless it is "custom"
      choose(mode) {
        this.mode = mode;
        if (mode !== "custom") this.from = this.to = "";
      },
      // Back to any date
      reset() {
        this.choose("");
      },
    }));

    // Chips for how many players a game fits, 1 to 6+, from the player counts in the address. Its
    // row starts `open`.
    Alpine.data("playerChips", (open, min, max) => ({
      open,
      min,
      max,
      // What the row says: "Fits 4 players", "Fits 6+ players", or another count as a range
      get summary() {
        const { min, max } = this;
        if (!min && !max) return "Any";
        if (min && max === min && Number(min) <= 6) return `Fits ${min} player${min === "1" ? "" : "s"}`;
        if (min === "6" && !max) return "Fits 6+ players";
        if (min && max) return min === max ? min : `${min}\u2013${max}`;
        return min ? `${min}+` : `up to ${max}`;
      },
      // Whether a player count is set
      get active() {
        return this.summary !== "Any";
      },
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

    // A row of checkbox choices (genres, platforms) whose checked values are `picked`, taken from
    // the checkboxes as the server rendered them. Its row starts `open`; `q` filters the options.
    Alpine.data("choiceRow", (open) => ({
      open,
      q: "",
      picked: [],
      // Every option's value and name, in order
      options: [],
      // Reads the options and the checked ones from the rendered checkboxes
      init() {
        const inputs = [...this.$el.querySelectorAll("input[data-label]")];
        this.options = inputs.map((input) => ({ value: input.value, label: input.dataset.label }));
        this.picked = inputs.filter((input) => input.checked).map((input) => input.value);
      },
      // What the row says: the chosen names in the options' order, "Drama, Crime" or "Drama +2"; "Any" for none
      get summary() {
        const names = this.options.filter((option) => this.picked.includes(option.value)).map((option) => option.label);
        if (names.length > 2) return `${names[0]} +${names.length - 1}`;
        return names.join(", ") || "Any";
      },
      // Whether any choice is checked
      get active() {
        return this.picked.length > 0;
      },
      // Unchecks one value, or every one without
      reset(value) {
        this.picked = value ? this.picked.filter((picked) => picked.toLowerCase() !== value.toLowerCase()) : [];
      },
    }));
  });
}
