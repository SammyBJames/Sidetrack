// Five rating stars with halves, each half a point out of ten, that save to `url`.
// Choosing the current rating again clears it. `open` is for a menu holding the stars.
// Pressing on the stars and dragging previews the rating under the pointer, and letting go
// sets it (ratingDrag), so a rating doesn't take a precise tap.
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
      // Sets the rating to `value`, or clears it when it's already that unless `toggle` is false
      setRating(value, toggle = true) {
        this.rating = toggle && this.rating === value ? null : value;
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

    // The stars' press and drag, inside a parent with `hovered` and `setRating` (ratingStars,
    // or mediaForm in the edit dialog). A tap sets the rating like a click, and a drag
    // never clears it. Keyboard clicks still go to the star halves.
    Alpine.data("ratingDrag", () => ({
      pressed: null,
      moved: false,
      // The rating out of ten under the pointer, in halves of the five stars
      valueAt(event) {
        const box = this.$el.getBoundingClientRect();
        return Math.min(10, Math.max(1, Math.ceil(((event.clientX - box.left) / box.width) * 10)));
      },
      press(event) {
        if (event.button !== 0) return;
        this.$el.setPointerCapture(event.pointerId);
        this.pressed = this.hovered = this.valueAt(event);
        this.moved = false;
      },
      drag(event) {
        if (this.pressed === null) return;
        this.hovered = this.valueAt(event);
        if (this.hovered !== this.pressed) this.moved = true;
      },
      release(event) {
        if (this.pressed === null) return;
        this.pressed = null;
        this.setRating(this.valueAt(event), !this.moved);
      },
      // A scroll or another gesture took over the press
      cancel() {
        this.pressed = this.hovered = null;
      },
    }));
  });
}
