export function makeLogger(service, client) {
  return async (level, message) => {
    try {
      await client.app.log({ body: { service, level, message } });
    } catch {}
  };
}