// Dialogs close on Escape, and on a click outside their sheet only when the press and the
// release both landed on the backdrop, so selecting text in a field and letting go over the
// backdrop, or pressing on the backdrop and letting go in the sheet, leaves them open.
// Either sends the `dialog-backdrop` a `dialog-dismiss` event, which its host listens for
// to close itself. Escape closes only the topmost open dialog.
if (!window.__dialogsRegistered) {
  window.__dialogsRegistered = true;
  let pressed = null;
  let released = null;
  document.addEventListener(
    "pointerdown",
    (event) => {
      pressed = event.target;
    },
    true,
  );
  document.addEventListener(
    "pointerup",
    (event) => {
      released = event.target;
    },
    true,
  );
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (target.classList?.contains("dialog-backdrop") && pressed === target && released === target) {
      target.dispatchEvent(new CustomEvent("dialog-dismiss"));
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    // Open dialogs have a box; of several, the one later in the page is drawn on top
    const open = [...document.querySelectorAll(".dialog-backdrop")].filter(
      (backdrop) => backdrop.getClientRects().length,
    );
    open.at(-1)?.dispatchEvent(new CustomEvent("dialog-dismiss"));
  });
}
