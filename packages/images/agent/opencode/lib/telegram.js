export async function telegramNotify({ $, text }) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const user = process.env.TELEGRAM_USER_ID;
  if (!token || !user) return false;
  try {
    await $`curl -s -X POST https://api.telegram.org/bot${token}/sendMessage -d chat_id=${user} -d text=${text}`.quiet();
    return true;
  } catch {
    return false;
  }
}