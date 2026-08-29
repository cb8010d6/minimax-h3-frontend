/**
 * Maps a job's prompt hash to the queue list's right-edge color line.
 *
 * The hash comes from the backend (generation/api.py::_prompt_hash): FNV-1a
 * 32-bit, 8 lowercase hex chars, computed over the prompt actually sent to
 * the server -- improved_prompt or raw_prompt, the same resolution the
 * render task uses (generation/tasks.py). The frontend only turns it into a
 * color: take the 32-bit value modulo 360 for a hue, with fixed
 * saturation/lightness so every line reads as "a color" rather than a
 * rainbow of different intensities. The same prompt (or a re-render of it)
 * always gets the same color, so related jobs are visually findable in a
 * long list; the mapping is cosmetic, not an identity.
 */
export function promptColor(promptHash: string): string {
  // parseInt yields a *signed* 32-bit int (values >= 0x80000000 go
  // negative); >>> 0 recovers the unsigned value before the modulo.
  const hue = (parseInt(promptHash, 16) >>> 0) % 360;
  return `hsl(${hue}, 60%, 50%)`;
}
