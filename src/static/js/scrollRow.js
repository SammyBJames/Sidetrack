// A row that scrolls sideways with left and right buttons. The track is the element with x-ref="track".
// Guard against re-registering when this script re-runs after an HTMX body swap.
if (!window.__scrollRowRegistered) {
  window.__scrollRowRegistered = true;
  document.addEventListener("alpine:init", () => {
    Alpine.data("scrollRow", () => ({
      atStart: true,
      atEnd: true,
      init() {
        this.$nextTick(() => this.update());
      },
      // Whether the track is scrolled to either end, which disables that button
      update() {
        const track = this.$refs.track;
        this.atStart = track.scrollLeft <= 1;
        this.atEnd = track.scrollLeft + track.clientWidth >= track.scrollWidth - 1;
      },
      // Scrolls by most of a screen, so the last card stays in view for context
      scroll(direction) {
        const track = this.$refs.track;
        track.scrollBy({ left: direction * track.clientWidth * 0.9, behavior: "smooth" });
      },
    }));
  });
}
