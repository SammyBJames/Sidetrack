// A save reloads the page in place by swapping its body (helpers.redirect_back). The page is
// empty for a moment during the swap, which loses the scroll position, and scroll anchoring
// then tends to jump to the bottom, so the position is put back once the new body is in.
if (!window.__softNavigationRegistered) {
  window.__softNavigationRegistered = true;
  let scrollY = null;
  const isSoftNavigation = (event) =>
    event.detail.target === document.body && event.detail.requestConfig?.headers?.["X-Soft-Navigation"];
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (isSoftNavigation(event)) scrollY = window.scrollY;
  });
  document.addEventListener("htmx:afterSwap", (event) => {
    if (!isSoftNavigation(event) || scrollY === null) return;
    window.scrollTo(0, scrollY);
    scrollY = null;
  });
}
