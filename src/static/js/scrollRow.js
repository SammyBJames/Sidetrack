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
      // Starts the track at `card`, or near the end at the card before it that keeps `card` in view, so a card always lines up with the title. The end is padded when the track can't scroll that far
      reveal(card) {
        const track = this.$refs.track;
        const style = getComputedStyle(track);
        const pad = parseFloat(style.scrollPaddingLeft);
        const origin = track.getBoundingClientRect().left - track.scrollLeft + pad;
        const starts = [...track.children].map((c) => c.getBoundingClientRect().left - origin);
        const want = card.getBoundingClientRect().left - origin;
        const max = track.scrollWidth - track.clientWidth;
        const fits = (start) => want + card.offsetWidth + 2 * pad <= start + track.clientWidth;
        let target = Math.max(...starts.filter((start) => start <= Math.min(want, max)));
        if (!fits(target)) target = starts.find((start) => start > target && fits(start));
        if (target > max) track.style.paddingRight = `${parseFloat(style.paddingRight) + target - max}px`;
        track.scrollLeft = target;
      },
    }));
  });
}
