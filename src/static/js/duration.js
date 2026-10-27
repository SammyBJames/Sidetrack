// A duration typed as "1h 30min", "1:30", "45min", "1h" or "1.5" (hours), moved by
// `delta` minutes and floored at `floor`, in the form the tracking forms accept.
// Returns null when the text is no duration, so the caller can leave it alone.
window.addMinutesToText = (text, delta, floor = 0) => {
  text = text.trim();
  let minutes = 0;
  if (text) {
    const clock = text.match(/^(\d+):(\d+)$/);
    const words = text.match(/^(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?$/);
    if (clock) minutes = clock[1] * 60 + +clock[2];
    else if (words) minutes = (words[1] || 0) * 60 + +(words[2] || 0);
    else if (!isNaN(text)) minutes = Math.round(text * 60);
    else return null;
  }
  minutes = Math.max(floor, minutes + delta);
  const hours = Math.floor(minutes / 60);
  return hours ? `${hours}h ${String(minutes % 60).padStart(2, "0")}min` : `${minutes}min`;
};
