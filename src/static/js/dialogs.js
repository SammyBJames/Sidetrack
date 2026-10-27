// Dialogs close on a click outside their sheet only when the press and the release both
// landed on the backdrop, so selecting text in a field and letting go over the backdrop,
// or pressing on the backdrop and letting go in the sheet, leaves them open. Such a click
// on a `dialog-backdrop` sends it a `dialog-dismiss` event, which its host listens for to
// close itself.
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
}
