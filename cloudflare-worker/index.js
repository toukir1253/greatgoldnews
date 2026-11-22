async function telegramSend(env, chatId, text) {
  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true })
  });
  if (!response.ok) throw new Error("Telegram sendMessage failed");
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("GreatNews signal webhook is running.", { status: 200 });
    }

    const suppliedSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
    if (!env.TELEGRAM_WEBHOOK_SECRET || suppliedSecret !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad Request", { status: 400 });
    }

    const message = update.message;
    if (!message || !message.chat || !message.from) {
      return new Response("OK", { status: 200 });
    }

    const chatId = String(message.chat.id);
    const ownerId = String(env.OWNER_CHAT_ID || "");
    if (message.chat.type !== "private" || !ownerId || chatId !== ownerId) {
      return new Response("OK", { status: 200 });
    }

    const text = String(message.text || "").trim();
    if (/^\/start(?:@\w+)?(?:\s|$)/i.test(text)) {
      await telegramSend(env, chatId, "GreatNews Bot চালু আছে। সিগন্যাল চাইলে /signal লিখো।");
      return new Response("OK", { status: 200 });
    }

    const commandMatch = text.match(/^\/(signal|calendar|forecast|analysis)(?:@\w+)?(?:\s|$)/i);
    if (!commandMatch) {
      if (text.startsWith("/")) await telegramSend(env, chatId, "কমান্ড: /signal, /calendar, /forecast, /analysis");
      return new Response("OK", { status: 200 });
    }
    const command = commandMatch[1].toLowerCase();

    const apiUrl = "https://api.github.com/repos/toukir1253/greatgoldnews/actions/workflows/signal.yml/dispatches";
    const gh = await fetch(apiUrl, {
      method: "POST",
      headers: {
        "accept": "application/vnd.github+json",
        "authorization": `Bearer ${env.GITHUB_TOKEN}`,
        "x-github-api-version": "2022-11-28",
        "content-type": "application/json",
        "user-agent": "greatnews-signal-worker"
      },
      body: JSON.stringify({ ref: "main", inputs: { manual_command: command, manual_signal: command === "signal" ? "true" : "false" } })
    });

    if (!gh.ok) {
      const detail = await gh.text().catch(() => "");
      console.log("GitHub dispatch failed:", gh.status, detail.slice(0, 500));
      await telegramSend(env, chatId, "⚠️ অনুরোধ চালু করা যায়নি। GitHub token, workflow permission বা Actions সেটিং পরীক্ষা করতে হবে।");
      return new Response("GitHub dispatch failed", { status: 502 });
    }

    await telegramSend(env, chatId, "✅ অনুরোধ পেয়েছি। মার্কেট বিশ্লেষণ চলছে। ভালো সেটআপ না পেলে কোনো BUY/SELL দেওয়া হবে না।");
    return new Response("OK", { status: 200 });
  }
};
