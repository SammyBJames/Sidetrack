// The log dialog (fill_log.html), which sits outside the cards it is opened from so
// their re-rendering can't disturb it. It remembers the card that opened it, which
// the response to the log replaces.
if (!window.__logFormRegistered) {
  window.__logFormRegistered = true;
  let card = null;
  window.addEventListener("open-log", (event) => {
    card = event.detail?.card ?? null;
  });

  document.addEventListener("alpine:init", () => {
    // `kind` is "book" or "game"; `total` is a book's page count, if known
    Alpine.data("logForm", (kind, total = null) => ({
      error: "",

      // Step the page by one within the book, or the time by half an hour (at least one)
      step(direction) {
        const field = this.$refs.amount;
        if (kind === "game") {
          const next = window.addMinutesToText(field.value, direction * 30, 30);
          if (next) field.value = next;
          return;
        }
        const next = Math.max(0, (parseInt(field.value, 10) || 0) + direction);
        field.value = total ? Math.min(next, total) : next;
      },

      // Post the log, replacing the card it was opened from with the response
      submit() {
        this.error = "";
        const form = this.$root;
        htmx.ajax("POST", form.action, {
          source: form,
          target: card ?? form,
          swap: card ? "outerHTML" : "none",
        });
      },

      // Show the first problem of a 422 response, whose body is {"errors": {field: [...]}}
      showError(xhr) {
        try {
          const errors = Object.values(JSON.parse(xhr.responseText).errors);
          this.error = errors[0][0].message;
        } catch {
          this.error = "Couldn't save that. Try again.";
        }
      },
    }));
  });
}
