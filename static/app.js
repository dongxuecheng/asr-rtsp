"use strict";
const $ = id => document.getElementById(id);
let selectedDate = "", serverToday = "", followingToday = true, query = "";
let requestId = 0, activeRequest = null, etag = null, fetchedKey = "";
let renderedKey = "", rendered = new Map(), unseen = 0, daySignature = "";
let pinnedToLatest = true;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function timestamp(startedAt, offset=0) {
  // Treat the server's local clock as a civil time, without browser TZ conversion.
  const time = Date.parse(startedAt.slice(0,23) + "Z") + offset*1000;
  return Number.isFinite(time) ? new Date(time).toISOString().slice(0,23).replace("T"," ") : startedAt.replace("T"," ");
}
function clock(startedAt, offset=0) {
  return timestamp(startedAt,offset).slice(11,19);
}
function buildRecord(record) {
  const article = el("div", "record");
  article.dataset.recordId = record.id;
  const segments = record.segments.length ? [...record.segments] : [{speaker:"未知说话人",start:0,end:record.duration,text:record.text}];
  segments.sort((a,b) => (a.start || 0) - (b.start || 0));
  for (const item of segments) {
    const line = el("p", "transcript-line");
    const begin = timestamp(record.started_at,item.start || 0);
    const end = timestamp(record.started_at,item.end ?? record.duration).slice(11);
    line.append(el("span", "transcript-time", `[${begin} - ${end}]`),document.createTextNode(" "),
                el("span", "transcript-speaker", `[${item.speaker || "未知说话人"}]`),document.createTextNode(" "),
                el("span", "transcript-text", item.text));
    article.append(line);
  }
  return article;
}
function nearLatest() {
  const reader = $("reader");
  return reader.scrollHeight - reader.scrollTop - reader.clientHeight <= 60;
}
function clearUnseen() {
  unseen = 0;
  $("newResults").hidden = true;
}
function updateReadingState() {
  const reader = $("reader");
  pinnedToLatest = reader.scrollHeight - reader.scrollTop - reader.clientHeight <= 2;
  $("readingState").textContent = !rendered.size ? "等待转写内容" : !pinnedToLatest ? "正在阅读历史" : $("follow").checked ? "已定位最新 · 自动跟随" : "已定位最新 · 跟随已暂停";
  if (!rendered.size || pinnedToLatest) clearUnseen();
  else {
    $("newResults").textContent = unseen ? `${unseen} 条新转写 · 回到最新 ↓` : "回到最新 ↓";
    $("newResults").hidden = false;
  }
}
function scrollToLatest(enableFollow=true) {
  if (enableFollow) $("follow").checked = true;
  $("reader").scrollTo({top:$("reader").scrollHeight,behavior:"auto"});
  clearUnseen();
  updateReadingState();
}
function resetView() {
  etag = null;
  fetchedKey = "";
  renderedKey = "";
  rendered.clear();
  $("records").replaceChildren();
  for (const id of ["total","characters","latest"]) $(id).textContent = "—";
  $("notice").hidden = false;
  $("notice").className = "";
  $("notice").textContent = "正在读取转写内容…";
  clearUnseen();
  updateReadingState();
}
function changeDay(day, follow=day === serverToday) {
  if (day !== selectedDate) resetView();
  selectedDate = day;
  followingToday = follow;
  $("date").value = day;
  refresh(true);
}
function showMeta(data) {
  serverToday = data.today;
  if (!selectedDate || (followingToday && selectedDate !== serverToday)) {
    resetView();
    selectedDate = serverToday;
    $("date").value = selectedDate;
  }
  $("timezone").textContent = data.timezone;
  $("today").classList.toggle("selected", followingToday);
  $("retention").textContent = data.retention_days ? `保留最近 ${data.retention_days} 天（含今天）。到期文本、录音与日志自动清理。` : "历史文本与录音持续保留，自动过期清理已关闭。";
  $("dayCount").textContent = `${data.days.length} 天`;
  const signature = JSON.stringify([selectedDate,data.days]);
  if (signature === daySignature) return;
  daySignature = signature;
  const fragment = document.createDocumentFragment();
  for (const day of data.days) {
    const button = el("button", `day ${day.date === selectedDate ? "active" : ""}`);
    button.type = "button";
    button.setAttribute("aria-current",day.date === selectedDate ? "date" : "false");
    button.append(el("span", "", day.date),el("small", "", `${day.count} 条`));
    button.addEventListener("click",() => changeDay(day.date));
    fragment.append(button);
  }
  $("days").replaceChildren(fragment);
}
function reconcile(items, key) {
  const sameView = renderedKey === key;
  const shouldFollow = !sameView || ($("follow").checked && nearLatest());
  const container = $("records"), next = new Map();
  let cursor = container.firstChild, added = 0;
  for (const record of items) {
    const signature = JSON.stringify([record.started_at,record.text,record.segments]);
    const previous = rendered.get(record.id);
    let node;
    if (previous && previous.signature === signature) node = previous.node;
    else {
      node = buildRecord(record);
      if (previous) {
        if (cursor === previous.node) cursor = previous.node.nextSibling;
        previous.node.remove();
      } else added++;
    }
    if (node === cursor) cursor = cursor.nextSibling;
    else container.insertBefore(node,cursor);
    next.set(record.id,{node,signature});
  }
  for (const [id, entry] of rendered) if (!next.has(id)) entry.node.remove();
  rendered = next;
  renderedKey = key;
  if (!items.length) clearUnseen();
  if (shouldFollow && added) {
    requestAnimationFrame(() => {if (renderedKey === key && (!sameView || $("follow").checked)) scrollToLatest(false);});
  } else if (sameView && added) {
    unseen += added;
    $("newResults").textContent = `${unseen} 条新转写 · 回到最新 ↓`;
    $("newResults").hidden = false;
  }
  updateReadingState();
}
async function get(url, signal, token=null) {
  const response = await fetch(url,{cache:"no-store",signal,headers:token ? {"If-None-Match":token} : {}});
  if (response.status === 304) return null;
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error || `读取失败 (${response.status})`);
  }
  return {data:await response.json(),etag:response.headers.get("ETag")};
}
async function refresh(force=false) {
  const generation = ++requestId;
  if (activeRequest) activeRequest.abort();
  const controller = new AbortController();
  activeRequest = controller;
  const timeout = setTimeout(() => controller.abort(),12000);
  $("refresh").disabled = true;
  try {
    const meta = await get("/api/days",controller.signal);
    if (generation !== requestId) return;
    showMeta(meta.data);
    const key = selectedDate + "\0" + query;
    const params = new URLSearchParams({date:selectedDate,q:query,all:"1"});
    const result = await get(`/api/records?${params}`,controller.signal,
                             !force && fetchedKey === key ? etag : null);
    if (generation !== requestId) return;
    if (result) {
      const data = result.data;
      etag = result.etag;
      fetchedKey = key;
      reconcile(data.items,key);
      $("total").textContent = data.summary.count;
      $("characters").textContent = data.summary.characters;
      $("latest").textContent = data.items.length ? clock(data.items[data.items.length-1].started_at) : "—";
      $("scope").textContent = query ? `关键词「${query}」的结果` : "当天全部内容";
      $("recordInfo").textContent = "按时间正序 · 最新在底部";
    }
    $("listTitle").textContent = `${selectedDate} 的转写内容`;
    $("export").href = `/api/export?${new URLSearchParams({date:selectedDate})}`;
    $("notice").hidden = rendered.size > 0;
    $("notice").className = "";
    $("notice").textContent = query ? "没有找到匹配的内容，请尝试其他关键词。" : "暂无有效转写。ASR 完成后，有内容的结果会自动显示在这里。";
    updateReadingState();
    $("connection").classList.remove("error");
    $("syncState").textContent = "自动更新 · 2 秒";
    $("lastSync").textContent = `最近同步 ${meta.data.checked_at || new Date().toLocaleTimeString("zh-CN",{hour12:false})}`;
  } catch(error) {
    if (generation !== requestId) return;
    $("connection").classList.add("error");
    $("syncState").textContent = "连接中断 · 正在重试";
    $("notice").hidden = false;
    $("notice").className = "error";
    const message = error.name === "AbortError" ? "请求超时" : error instanceof TypeError ? "暂时无法连接服务" : error.message;
    $("notice").textContent = `${message}。将自动重试，已显示的文字会保留。`;
  } finally {
    clearTimeout(timeout);
    if (generation === requestId) {activeRequest=null; $("refresh").disabled=false;}
  }
}
function search() {
  const next = $("query").value.trim();
  if (next !== query) resetView();
  query = next;
  refresh(true);
}
$("searchForm").addEventListener("submit",event => {event.preventDefault(); search();});
$("query").addEventListener("search",() => {if (!$("query").value) search();});
$("date").addEventListener("change",() => {if ($("date").value) changeDay($("date").value);});
$("today").addEventListener("click",() => {changeDay(serverToday || selectedDate,true); scrollToLatest();});
$("refresh").addEventListener("click",() => refresh(true));
$("follow").addEventListener("change",() => {if ($("follow").checked) scrollToLatest(); else updateReadingState();});
$("newResults").addEventListener("click",() => scrollToLatest());
$("reader").addEventListener("scroll",updateReadingState,{passive:true});
if (window.ResizeObserver) new ResizeObserver(() => {
  if (pinnedToLatest && $("follow").checked) $("reader").scrollTop = $("reader").scrollHeight;
}).observe($("reader"));
document.addEventListener("visibilitychange",() => {if (!document.hidden) refresh();});
window.addEventListener("online",() => refresh());
setInterval(() => {if (!document.hidden && !activeRequest) refresh();},2000);
refresh();
