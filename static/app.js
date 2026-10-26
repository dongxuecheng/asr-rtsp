"use strict";
const $ = id => document.getElementById(id);
let selectedDate = "", query = "", page = 1, total = 0, requestId = 0;
const limit = 50;
const statuses = {done:"已转写", failed:"识别失败 · 音频已保留", pending:"识别中", empty:"未识别到文本"};
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function duration(seconds) {
  const n = Math.round(seconds);
  return n < 60 ? `${n} 秒` : `${Math.floor(n/60)} 分 ${n%60} 秒`;
}
async function get(url) {
  const response = await fetch(url, {cache:"no-store"});
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error || `读取失败 (${response.status})`);
  }
  return response.json();
}
function buildRecord(record) {
  const article = el("article", "record");
  const time = el("div", "time", record.started_at.slice(11,19));
  time.append(el("small", "", duration(record.duration)));
  const card = el("div", "card");
  const top = el("div", "card-top");
  top.append(el("span", `status ${record.status}`, statuses[record.status] || record.status), el("span", "record-id", record.id.slice(0,8)));
  card.append(top);
  const audio = el("audio");
  audio.controls = true;
  audio.preload = "none";
  audio.src = record.audio_url;
  audio.setAttribute("aria-label", `${record.started_at} 的录音`);
  audio.addEventListener("play", () => {
    document.querySelectorAll("audio").forEach(other => { if(other !== audio) other.pause(); });
  });
  if (record.segments.length) {
    const segments = el("div", "segments");
    for (const item of record.segments) {
      const line = el("div", "segment"), speaker = el("div", "speaker", item.speaker);
      const jump = el("button", "jump", `▶ +${item.start.toFixed(1)}s`);
      jump.type = "button";
      jump.setAttribute("aria-label", `播放 ${item.speaker} 在 ${item.start.toFixed(1)} 秒的语音`);
      jump.addEventListener("click", () => {
        const play = () => {audio.currentTime = item.start; audio.play().catch(() => {});};
        if (audio.readyState >= 1) play();
        else {audio.addEventListener("loadedmetadata", play, {once:true}); audio.load();}
      });
      speaker.append(jump);
      line.append(speaker, el("p", "", item.text));
      segments.append(line);
    }
    card.append(segments);
  } else {
    card.append(el("p", `record-note ${record.error ? "error" : ""}`, record.error || (record.status === "pending" ? "正在等待 ASR 返回结果，音频已经保存。" : "检测到声音，但 ASR 没有返回有效文本。")));
  }
  const player = el("div", "player"), download = el("a", "", "下载 WAV ↗");
  download.href = record.audio_url;
  download.download = "";
  player.append(audio, download);
  card.append(player);
  article.append(time, card);
  return article;
}
async function refreshDays() {
  const data = await get("/api/days");
  if (!selectedDate) {
    selectedDate = data.today;
    $("date").value = selectedDate;
  }
  $("timezone").textContent = data.timezone;
  $("retention").textContent = data.retention_days ? `按采集端日期保留最近 ${data.retention_days} 天（含今天）。到期文本、录音与日志会自动清理。` : "自动清理已关闭，历史文本与录音持续保留。";
  $("dayCount").textContent = `${data.days.length} 天`;
  const fragment = document.createDocumentFragment();
  for (const day of data.days) {
    const button = el("button", `day ${day.date === selectedDate ? "active" : ""}`);
    button.type = "button";
    button.setAttribute("aria-current", day.date === selectedDate ? "date" : "false");
    button.append(el("span", "", day.date), el("small", "", `${day.count} 条`));
    button.addEventListener("click", () => {selectedDate = day.date; $("date").value = selectedDate; page = 1; refresh();});
    fragment.append(button);
  }
  $("days").replaceChildren(fragment);
}
async function refresh() {
  const generation = ++requestId;
  $("refresh").disabled = true;
  try {
    await refreshDays();
    const params = new URLSearchParams({date:selectedDate, q:query, page, limit});
    const data = await get(`/api/records?${params}`);
    if (generation !== requestId) return;
    total = data.summary.count;
    if (page > 1 && (page-1)*limit >= total) {page = Math.max(1, Math.ceil(total/limit)); return refresh();}
    $("total").textContent = total;
    $("duration").textContent = duration(data.summary.duration);
    $("failed").textContent = data.summary.failed;
    $("pending").textContent = `${data.summary.pending} 条识别中 · ${data.summary.empty} 条无文本`;
    $("scope").textContent = query ? `关键词「${query}」的结果` : "当日全部记录";
    $("listTitle").textContent = `${selectedDate} 的记录`;
    $("pageInfo").textContent = `${page} / ${Math.max(1, Math.ceil(total/limit))} 页`;
    $("export").href = `/api/export?${new URLSearchParams({date:selectedDate})}`;
    $("notice").hidden = data.items.length > 0;
    $("notice").className = "";
    $("notice").textContent = query ? "没有找到匹配的内容，请尝试其他关键词。" : "这一天还没有语音记录。采集到有效人声后，记录会显示在这里。";
    $("records").replaceChildren(...data.items.map(buildRecord));
    $("prev").disabled = page <= 1;
    $("next").disabled = page*limit >= total;
  } catch(error) {
    if (generation !== requestId) return;
    $("notice").hidden = false;
    $("notice").className = "error";
    $("notice").textContent = `${error.message}，请点击刷新重试。`;
  } finally {
    if (generation === requestId) $("refresh").disabled = false;
  }
}
$("searchForm").addEventListener("submit", event => {event.preventDefault(); query = $("query").value.trim(); page=1; refresh();});
$("query").addEventListener("search", () => {if(!$("query").value) {query=""; page=1; refresh();}});
$("date").addEventListener("change", () => {if($("date").value) {selectedDate=$("date").value; page=1; refresh();}});
$("refresh").addEventListener("click", refresh);
$("prev").addEventListener("click", () => {if(page>1) {page--; refresh();}});
$("next").addEventListener("click", () => {if(page*limit<total) {page++; refresh();}});
setInterval(() => {
  if (!document.hidden && !Array.from(document.querySelectorAll("audio")).some(a => !a.paused) && !$("refresh").disabled) refresh();
}, 30000);
refresh();
