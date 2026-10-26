// A media type's list filters, kept in the hidden fields of the form with x-ref="form", which
// reloads the results on each change. The sorts come from the JSON in the element with
// id="library-sorts", each with its usual direction and how either direction reads.
// Guard against re-registering when this script re-runs after an HTMX body swap.
if (!window.__libraryFiltersRegistered) {
  window.__libraryFiltersRegistered = true;
  document.addEventListener("alpine:init", () => {
    Alpine.data("libraryFilters", (state) => ({
      ...state,
      sorts: JSON.parse(document.getElementById("library-sorts").textContent),
      // The selected sort's name and how its direction reads
      get sortLabel() {
        return this.sortChoice().label;
      },
      get orderLabel() {
        const [ascending, descending] = this.sortChoice().order_labels;
        return this.order === "asc" ? ascending : descending;
      },
      // How many filters besides the status narrow the list
      get filterCount() {
        return (this.rated ? 1 : 0) + (this.rewatched ? 1 : 0);
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
      clearFilters() {
        Object.assign(this, { search: "", rated: "", rewatched: false, status: "All" });
        this.submit();
      },
      // Waits for the fields to take the new values before sending them
      submit() {
        this.$nextTick(() => htmx.trigger(this.$refs.form, "submit"));
      },
    }));
  });
}
