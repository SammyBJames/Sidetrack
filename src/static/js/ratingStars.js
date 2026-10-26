// Five rating stars with halves, each half a point out of ten, that save to `url`.
// Choosing the current rating again clears it. `open` is for a menu holding the stars.
// Other stars on the page for the same entry, e.g. in the header and in its tile,
// follow a rating set by any of them.
// Guard against re-registering when this script re-runs after an HTMX body swap.
if (!window.__ratingStarsRegistered) {
  window.__ratingStarsRegistered = true;
  document.addEventListener("alpine:init", () => {
    Alpine.data("ratingStars", (rating, url, csrfToken) => ({
      rating,
      hovered: null,
      open: false,
      init() {
        this.follow = (event) => {
          if (event.detail.url === url) this.rating = event.detail.rating;
        };
        window.addEventListener("rating-changed", this.follow);
      },
      destroy() {
        window.removeEventListener("rating-changed", this.follow);
      },
      // How full a star is, as a percentage, for the hovered rating or else the saved one
      fill(star) {
        const value = this.hovered ?? this.rating ?? 0;
        return value >= star * 2 ? 100 : value >= star * 2 - 1 ? 50 : 0;
      },
      setRating(value) {
        this.rating = this.rating === value ? null : value;
        this.hovered = null;
        this.open = false;
        window.dispatchEvent(
          new CustomEvent("rating-changed", { detail: { url, rating: this.rating } }),
        );
        htmx.ajax("POST", url, {
          values: { score: this.rating ?? "" },
          headers: { "X-CSRFToken": csrfToken },
          swap: "none",
        });
      },
    }));
  });
}
