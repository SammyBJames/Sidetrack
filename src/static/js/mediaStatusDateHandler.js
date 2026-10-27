// Guard against re-registering when this script re-runs after an HTMX body swap.
if (!window.__mediaFormRegistered) {
  window.__mediaFormRegistered = true;
  document.addEventListener("alpine:init", () => {
  // The tracking form: auto-filled dates, a star rating out of ten in halves of a point
  // (`score`), a progress stepper up to `total` if known, and Today/Clear for dates.
  Alpine.data("mediaForm", (score = null, total = null) => ({
    score,
    hovered: null,
    // The "Advanced" fold: whether it is open and the dates its summary follows
    open: false,
    extra: { start_date: "", end_date: "" },
    autoFilled: {
      start_date: false,
      end_date: false,
    },
    // Track original values to detect intentionally empty dates
    original: {
      status: null,
      start_date: null,
      end_date: null,
    },

    // Wire the status radios to the auto-filled dates and seed `extra`
    init() {
      // Status is a group of radios; its current value is the checked one
      const root = this.$el;
      const statusField = {
        get value() {
          return root.querySelector('[name="status"]:checked')?.value;
        },
      };
      const endDateField = this.$el.querySelector('[name="end_date"]');
      const startDateField = this.$el.querySelector('[name="start_date"]');
      const instanceIdField = this.$el.querySelector('[name="instance_id"]');

      // Check if this is a new form (no instance_id) vs editing existing record
      const isNewForm = !instanceIdField || !instanceIdField.value;

      // Store original values for edit forms
      if (!isNewForm) {
        this.original.status = statusField?.value || null;
        this.original.start_date = startDateField?.value || null;
        this.original.end_date = endDateField?.value || null;
      }
      this.syncOptionalDateField(startDateField);
      this.syncOptionalDateField(endDateField);

      // Initial load handling - only auto-fill for new forms
      // For existing records, respect the saved values (even if empty)
      if (
        isNewForm &&
        statusField &&
        statusField.value === "Completed" &&
        endDateField &&
        !endDateField.value
      ) {
        this.setDateFieldValue(endDateField, this.getCurrentDateTime(endDateField));
        this.autoFilled.end_date = true;
      } else if (
        isNewForm &&
        statusField &&
        statusField.value === "In progress" &&
        startDateField &&
        !startDateField.value
      ) {
        this.setDateFieldValue(
          startDateField,
          this.getCurrentDateTime(startDateField),
        );
        this.autoFilled.start_date = true;
      }

      // Status change handler
      if (statusField) {
        this.$el.addEventListener("change", (e) => {
          if (e.target.name !== "status") return;
          const status = e.target.value;

          // Clear previously auto-filled fields when status changes
          if (this.autoFilled.start_date && startDateField) {
            this.setDateFieldValue(startDateField, "");
            this.autoFilled.start_date = false;
          }
          if (this.autoFilled.end_date && endDateField) {
            this.setDateFieldValue(endDateField, "");
            this.autoFilled.end_date = false;
          }

          // For edit forms: don't auto-fill if returning to original status
          // where the date was intentionally left empty
          const isReturningToOriginalCompleted =
            status === "Completed" &&
            this.original.status === "Completed" &&
            this.original.end_date === null;

          const isReturningToOriginalInProgress =
            status === "In progress" &&
            this.original.status === "In progress" &&
            this.original.start_date === null;

          // Set new dates based on new status
          if (
            status === "Completed" &&
            endDateField &&
            !endDateField.value &&
            !isReturningToOriginalCompleted
          ) {
            this.setDateFieldValue(
              endDateField,
              this.getCurrentDateTime(endDateField),
            );
            this.autoFilled.end_date = true;
          } else if (
            status === "In progress" &&
            startDateField &&
            !startDateField.value &&
            !isReturningToOriginalInProgress
          ) {
            this.setDateFieldValue(
              startDateField,
              this.getCurrentDateTime(startDateField),
            );
            this.autoFilled.start_date = true;
          }

          this.syncOptionalDateField(startDateField);
          this.syncOptionalDateField(endDateField);
        });
      }
      this.syncExtra();
    },

    // Copy the dates into `extra`, which the row's summary follows
    syncExtra() {
      for (const name of Object.keys(this.extra)) {
        this.extra[name] = this.$root.querySelector(`[name="${name}"]`)?.value ?? "";
      }
    },

    // A date field's value as "Mar 1", with the year when it isn't this year's
    shortDate(value) {
      const [year, month, day] = value.slice(0, 10).split("-").map(Number);
      const options = { month: "short", day: "numeric" };
      if (year !== new Date().getFullYear()) options.year = "numeric";
      return new Date(year, month - 1, day).toLocaleDateString(undefined, options);
    },

    // The dates in a few words: nothing, one with what it is, the day when both are the same, or the two as a range
    get summary() {
      const { start_date: start, end_date: end } = this.extra;
      if (start && end) {
        if (start.slice(0, 10) === end.slice(0, 10)) return this.shortDate(start);
        return `${this.shortDate(start)} \u2013 ${this.shortDate(end)}`;
      }
      if (start) return `Started ${this.shortDate(start)}`;
      if (end) return `Finished ${this.shortDate(end)}`;
      return "";
    },

    // How full a star is, as a percentage, for the hovered rating or else the form's
    fill(star) {
      const value = this.hovered ?? this.score ?? 0;
      return value >= star * 2 ? 100 : value >= star * 2 - 1 ? 50 : 0;
    },

    // Choosing the current rating again clears it (rating_stars.html calls this)
    setRating(value) {
      this.score = this.score === value ? null : value;
      this.hovered = null;
    },

    // Step the progress by `delta`, from 0 up to the total when there is one
    bump(delta) {
      const field = this.$refs.progress;
      const next = Math.max(0, (parseInt(field.value, 10) || 0) + delta);
      field.value = total ? Math.min(next, total) : next;
    },

    // Add `delta` minutes to the progress, a duration as typed in the form
    addMinutes(delta) {
      const next = window.addMinutesToText(this.$refs.progress.value, delta);
      if (next) this.$refs.progress.value = next;
    },

    // Fill a date field with now, or empty it, as if the user had typed it
    setDate(name, now) {
      const field = this.$root.querySelector(`[name="${name}"]`);
      this.setDateFieldValue(field, now ? this.getCurrentDateTime(field) : "");
      // A date set by hand is no longer one to clear when the status changes
      if (this.autoFilled[name]) this.autoFilled[name] = false;
    },

    setDateFieldValue(field, value) {
      if (!field) {
        return;
      }

      field.value = value;
      this.syncOptionalDateField(field);

      // WebKit can keep stale validity state after scripted datetime changes.
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
    },

    syncOptionalDateField(field) {
      if (!field) {
        return;
      }

      field.required = false;
      field.removeAttribute("required");
      field.setCustomValidity("");

      const value = field.value;
      field.defaultValue = "";
      field.value = value;
    },

    getCurrentDateTime(field) {
      const now = new Date();
      const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);

      if (field.type === "datetime-local") {
        return local.toISOString().slice(0, 16);
      }

      // "date" type and fallback both use the local date.
      return local.toISOString().slice(0, 10);
    },
  }));
  });
}
