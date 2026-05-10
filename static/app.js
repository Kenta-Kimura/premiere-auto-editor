const form = document.querySelector("#analysisForm");
const messages = document.querySelector("#messages");
const result = document.querySelector("#result");
const startButton = document.querySelector("#startButton");
const stopPartialButton = document.querySelector("#stopPartialButton");
const abortButton = document.querySelector("#abortButton");
const progressLabel = document.querySelector("#progressLabel");
const progressPercent = document.querySelector("#progressPercent");
const progressBar = document.querySelector("#progressBar");
const previewPanel = document.querySelector("#previewPanel");
const previewTabs = document.querySelector("#previewTabs");
const previewTable = document.querySelector("#previewTable");
const exportButton = document.querySelector("#exportButton");
const chatgptPanel = document.querySelector("#chatgptPanel");
const chatgptPrompt = document.querySelector("#chatgptPrompt");
const copyPromptButton = document.querySelector("#copyPromptButton");
const correctedTranscript = document.querySelector("#correctedTranscript");
const applyCorrectionsButton = document.querySelector("#applyCorrectionsButton");

let currentJobId = null;
let currentPreview = null;
let activePreviewName = "summary.csv";

async function loadSystem() {
  const response = await fetch("/api/system");
  const system = await response.json();
  const missing = [];
  if (!system.ffmpeg) missing.push("FFmpeg");
  if (!system.ffprobe) missing.push("FFprobe");
  if (!system.whisper) missing.push("Whisper");
  if (missing.length) {
    result.textContent = `不足している外部ツールがあります: ${missing.join(", ")}\n解析前にセットアップを確認してください。`;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  const payload = {
    input_path: data.get("input_path"),
    output_dir: data.get("output_dir"),
    terms_path: data.get("terms_path"),
    enable_transcription: data.get("enable_transcription") === "on",
    silence_threshold_db: data.get("silence_threshold_db"),
    min_silence_duration: data.get("min_silence_duration"),
  };
  setRunning(true);
  previewPanel.hidden = true;
  chatgptPanel.hidden = true;
  chatgptPrompt.value = "";
  correctedTranscript.value = "";
  currentPreview = null;
  messages.innerHTML = "";
  result.textContent = "解析を開始しました。結果はまだ最終出力されません。";
  updateProgress({ percent: 1, label: "解析を開始しました" });

  const response = await fetch("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const { job_id } = await response.json();
  currentJobId = job_id;
  pollJob(job_id);
});

stopPartialButton.addEventListener("click", () => stopJob("partial"));
abortButton.addEventListener("click", () => stopJob("abort"));
exportButton.addEventListener("click", exportCurrentJob);
copyPromptButton.addEventListener("click", copyChatgptPrompt);
applyCorrectionsButton.addEventListener("click", applyTranscriptCorrections);

for (const button of document.querySelectorAll("[data-dialog]")) {
  button.addEventListener("click", async () => {
    const oldText = button.textContent;
    button.textContent = "選択中...";
    button.disabled = true;
    const response = await fetch("/api/dialog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: button.dataset.dialog }),
    });
    const data = await response.json();
    button.textContent = oldText;
    button.disabled = false;
    if (data.path) {
      const target = dialogTarget(button.dataset.dialog);
      form.elements[target].value = data.path;
    } else if (data.error) {
      result.textContent = `選択ダイアログを開けませんでした。\n${data.error}`;
    }
  });
}

for (const zone of document.querySelectorAll(".dropZone")) {
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("dragging");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("dragging");
    const path = pathFromDrop(event.dataTransfer);
    if (path) {
      form.elements[zone.dataset.target].value = path;
    } else {
      result.textContent =
        "このドラッグ操作ではパスを取得できませんでした。選択ボタンか、Finderでパスをコピーして貼り付けてください。";
    }
  });
}

async function stopJob(mode) {
  if (!currentJobId) return;
  const label = mode === "partial" ? "ここまでの結果を確認できる状態にします。" : "解析を中止します。";
  result.textContent = label;
  stopPartialButton.disabled = true;
  abortButton.disabled = true;
  await fetch(`/api/jobs/${currentJobId}/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
}

async function pollJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const job = await response.json();
  updateProgress(job.progress);
  messages.innerHTML = "";
  for (const message of job.messages || []) {
    const item = document.createElement("li");
    item.textContent = message;
    messages.appendChild(item);
  }

  if (job.status === "completed") {
    currentJobId = jobId;
    result.textContent = formatResult("解析完了。CSVを確認してから出力してください。", job.result);
    updateProgress({ percent: 100, label: "確認待ち" });
    setChatgptPrompt(job.result.chatgpt_prompt);
    showPreview(job.result.preview);
    previewPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    setRunning(false, { keepJob: true });
    loadSystem();
    return;
  }

  if (job.status === "stopped") {
    currentJobId = jobId;
    result.textContent = formatResult("停止しました。ここまでのCSVを確認できます。", job.result);
    updateProgress(job.progress || { percent: 100, label: "確認待ち" });
    setChatgptPrompt(job.result.chatgpt_prompt);
    showPreview(job.result.preview);
    previewPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    setRunning(false, { keepJob: true });
    loadSystem();
    return;
  }

  if (job.status === "aborted") {
    result.textContent = job.error || "解析を中止しました。";
    updateProgress(job.progress || { percent: 0, label: "中止" });
    setRunning(false);
    return;
  }

  if (job.status === "failed") {
    result.textContent = `失敗\n${job.error}`;
    updateProgress(job.progress || { percent: 0, label: "失敗" });
    setRunning(false);
    return;
  }

  window.setTimeout(() => pollJob(jobId), 1000);
}

async function exportCurrentJob() {
  if (!currentJobId) return;
  exportButton.disabled = true;
  exportButton.textContent = "出力中...";
  try {
    const outputDir = form.elements.output_dir.value;
    const response = await fetch(`/api/jobs/${currentJobId}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ output_dir: outputDir }),
    });
    const data = await response.json();
    if (data.ok) {
      result.innerHTML = `出力しました。<br>出力先: ${escapeHtml(data.output_dir)}<br><a href="${data.download_url}" download>ZIPをダウンロード</a>`;
    } else {
      result.textContent = `出力できませんでした。\n${data.error}`;
    }
  } catch (error) {
    result.textContent = `出力できませんでした。\n${error}`;
  } finally {
    exportButton.disabled = false;
    exportButton.textContent = "確認して出力";
  }
}

async function copyChatgptPrompt() {
  if (!chatgptPrompt.value) return;
  try {
    await navigator.clipboard.writeText(chatgptPrompt.value);
    copyPromptButton.textContent = "コピー済み";
    window.setTimeout(() => {
      copyPromptButton.textContent = "コピー";
    }, 1400);
  } catch (_error) {
    chatgptPrompt.focus();
    chatgptPrompt.select();
    result.textContent = "自動コピーできませんでした。プロンプト欄を選択してコピーしてください。";
  }
}

async function applyTranscriptCorrections() {
  if (!currentJobId) return;
  const csvText = correctedTranscript.value.trim();
  if (!csvText) {
    result.textContent = "補完済みCSVを貼り付けてください。";
    return;
  }
  applyCorrectionsButton.disabled = true;
  applyCorrectionsButton.textContent = "反映中...";
  try {
    const response = await fetch(`/api/jobs/${currentJobId}/apply-transcript`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csv_text: csvText }),
    });
    const data = await response.json();
    if (data.ok) {
      currentPreview = data.preview;
      setChatgptPrompt(data.chatgpt_prompt);
      showPreview(data.preview);
      activePreviewName = "transcript.csv";
      renderPreviewTabs();
      renderPreviewTable();
      result.textContent = `補完済みCSVを反映しました。\n反映行数: ${data.rows}`;
    } else {
      result.textContent = `補完を反映できませんでした。\n${data.error}`;
    }
  } catch (error) {
    result.textContent = `補完を反映できませんでした。\n${error}`;
  } finally {
    applyCorrectionsButton.disabled = false;
    applyCorrectionsButton.textContent = "補完を反映";
  }
}

function formatResult(label, data) {
  return `${label}\n一時解析先: ${data.output_dir}\nクリップ数: ${data.clip_count}\nカット候補: ${data.cut_candidates}`;
}

function setRunning(running, options = {}) {
  startButton.disabled = running;
  stopPartialButton.disabled = !running;
  abortButton.disabled = !running;
  if (!running && !options.keepJob) currentJobId = null;
}

function updateProgress(progress) {
  const percent = Math.max(0, Math.min(100, Number(progress?.percent || 0)));
  progressBar.style.width = `${percent}%`;
  progressPercent.textContent = `${Math.round(percent)}%`;
  progressLabel.textContent = progress?.label || "待機中";
}

function showPreview(preview) {
  currentPreview = preview || {};
  previewPanel.hidden = false;
  if (!Object.keys(currentPreview).length) {
    previewTabs.innerHTML = "";
    previewTable.innerHTML = `<p class="emptyPreview">プレビューできるCSVがありません。ページを再読み込みして、もう一度解析してください。</p>`;
    return;
  }
  activePreviewName = currentPreview[activePreviewName] ? activePreviewName : Object.keys(currentPreview)[0];
  renderPreviewTabs();
  renderPreviewTable();
}

function setChatgptPrompt(prompt) {
  const text = String(prompt || "");
  chatgptPrompt.value = text;
  chatgptPanel.hidden = !text;
}

function renderPreviewTabs() {
  previewTabs.innerHTML = "";
  for (const name of Object.keys(currentPreview || {})) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = name === activePreviewName ? "tab active" : "tab";
    button.textContent = `${name} (${currentPreview[name].total_rows})`;
    button.addEventListener("click", () => {
      activePreviewName = name;
      renderPreviewTabs();
      renderPreviewTable();
    });
    previewTabs.appendChild(button);
  }
}

function renderPreviewTable() {
  const data = currentPreview?.[activePreviewName];
  if (!data) {
    previewTable.innerHTML = "";
    return;
  }
  if (!data.rows.length) {
    previewTable.innerHTML = `<p class="emptyPreview">${activePreviewName} は空です。</p>`;
    return;
  }
  const headers = data.headers;
  const head = headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("");
  const body = data.rows
    .map((row) => `<tr>${headers.map((header) => `<td>${escapeHtml(row[header] || "")}</td>`).join("")}</tr>`)
    .join("");
  previewTable.innerHTML = `<div class="tableScroller"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function dialogTarget(kind) {
  if (kind === "output") return "output_dir";
  if (kind === "terms") return "terms_path";
  return "input_path";
}

function pathFromDrop(dataTransfer) {
  const uri = dataTransfer.getData("text/uri-list") || dataTransfer.getData("URL");
  const text = dataTransfer.getData("text/plain");
  const candidate = (uri || text || "").split(/\r?\n/).find((line) => line && !line.startsWith("#"));
  if (candidate) return decodeFilePath(candidate.trim());
  const file = dataTransfer.files && dataTransfer.files[0];
  if (file && file.path) return file.path;
  if (file && file.name) return file.name;
  return "";
}

function decodeFilePath(value) {
  if (value.startsWith("file://")) {
    return decodeURIComponent(value.replace("file://", ""));
  }
  return value;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

loadSystem();
