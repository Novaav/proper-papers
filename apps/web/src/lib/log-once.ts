// A logger that writes each distinct message once per process, so a
// persistent misconfiguration is reported without flooding the logs on every
// request (which could hide other events).

export function createOnceLogger(
  log: (message: string) => void,
): (message: string) => void {
  const seen = new Set<string>();
  return (message) => {
    if (seen.has(message)) return;
    seen.add(message);
    log(message);
  };
}
