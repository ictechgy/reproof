// Choose the authenticated issue console before starting any legacy polling.
try {
  const response = await fetch("/api/mode", { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) throw new Error("Coordinator mode is unavailable");
  const mode = await response.json();
  if (mode.mode === "shared") {
    const { mountIssueConsole } = await import("./issue.js");
    mountIssueConsole(document.querySelector(".app-shell"), mode);
  } else if (mode.mode === "legacy") {
    document.querySelector(".app-shell").hidden = false;
    await import("./app.js");
  } else throw new Error("Unsupported coordinator mode");
} catch {
  const message = document.createElement("p");
  message.className = "boot-error"; message.setAttribute("role", "alert");
  message.textContent = "The console could not connect. Reload this page after the coordinator is available.";
  document.body.replaceChildren(message);
}
