const streamsByTab = new Map();
const pageStateByTab = new Map();
const autoIntentByTab = new Map();
const primaryTabByWindow = new Map();
const historyByTab = new Map();
let lastStateTabId = null;
let lastCommandId = 0;
const SUPPORTED_BASES = [
  "https://yflix.to/",
  "https://dashflix.top/"
];
const DEFAULT_BROWSER_URL = SUPPORTED_BASES[0];
const queueEndpoints = [
  "http://downloader-app:8765/api/browser/queue",
  "http://isambard-app:8765/api/browser/queue",
  "http://app:8765/api/browser/queue",
  "http://127.0.0.1:8765/api/browser/queue",
  "http://localhost:8765/api/browser/queue"
];

function normalizeTitle(metadata) {
  const rawTitle = (metadata.raw_title || "Untitled")
    .trim()
    .replace(/\s*[-|]\s*Y?Flix.*$/i, "")
    .replace(/\s*[-|]\s*DashFlix.*$/i, "")
    .trim();
  if (metadata.season && metadata.episode) {
    const season = String(metadata.season).padStart(2, "0");
    const episode = String(metadata.episode).padStart(2, "0");
    return `${rawTitle} S${season}E${episode}`;
  }
  return rawTitle;
}

function rememberStream(tabId, details) {
  if (tabId < 0 || !details.url || !details.url.toLowerCase().includes(".m3u8")) {
    return;
  }
  const existing = streamsByTab.get(tabId) || [];
  if (!existing.find((item) => item.url === details.url)) {
    existing.unshift({ url: details.url });
    autoQueueStream(tabId, details.url).catch(() => {});
  }
  streamsByTab.set(tabId, existing.slice(0, 20));
  publishState(tabId).catch(() => {});
}

function isSupportedUrl(url) {
  return typeof url === "string" && SUPPORTED_BASES.some((base) => url.startsWith(base));
}

function hasAutoIntentParams(url) {
  try {
    return new URL(url).searchParams.has("isambard_title");
  } catch (_error) {
    return false;
  }
}

function autoIntentFromUrl(url) {
  try {
    const parsed = new URL(url);
    const params = parsed.searchParams;
    if (!params.has("isambard_title")) {
      return null;
    }
    return {
      title: params.get("isambard_title") || params.get("keyword") || "",
      year: params.get("isambard_year") || "",
      media_type: params.get("isambard_media_type") || "movie",
      season: params.get("isambard_season") || "",
      episode: params.get("isambard_episode") || "",
      poster_url: params.get("isambard_poster_url") || "",
      backdrop_url: params.get("isambard_backdrop_url") || ""
    };
  } catch (_error) {
    return null;
  }
}

function isAutoIntentTab(tab) {
  return hasAutoIntentParams(tab?.url || tab?.pendingUrl || "");
}

function stateEndpoints() {
  return [
    "http://downloader-app:8765/api/browser/state",
    "http://isambard-app:8765/api/browser/state",
    "http://app:8765/api/browser/state",
    "http://127.0.0.1:8765/api/browser/state",
    "http://localhost:8765/api/browser/state"
  ];
}

function commandEndpoints() {
  return [
    "http://downloader-app:8765/api/browser/command",
    "http://isambard-app:8765/api/browser/command",
    "http://app:8765/api/browser/command",
    "http://127.0.0.1:8765/api/browser/command",
    "http://localhost:8765/api/browser/command"
  ];
}

function pickPrimaryTab(tabs) {
  return tabs.find((tab) => isSupportedUrl(tab.url)) || tabs[0] || null;
}

function updatePrimaryTab(windowId, tabId) {
  if (windowId == null || tabId == null) {
    return;
  }
  primaryTabByWindow.set(windowId, tabId);
}

function keepSingleBrowserTab(windowId, preferredTabId, nextUrl) {
  if (windowId == null) {
    return;
  }
  chrome.tabs.query({ windowId }, (tabs) => {
    if (!tabs.length) {
      return;
    }
    const preferredTab = tabs.find((tab) => tab.id === preferredTabId);
    const currentPrimary = tabs.find((tab) => tab.id === primaryTabByWindow.get(windowId));
    const primaryTab = preferredTab || currentPrimary || pickPrimaryTab(tabs);
    if (!primaryTab || primaryTab.id == null) {
      return;
    }
    updatePrimaryTab(windowId, primaryTab.id);
    const extraTabs = tabs
      .filter((tab) => tab.id !== primaryTab.id && !isAutoIntentTab(tab))
      .map((tab) => tab.id)
      .filter((tabId) => tabId != null);
    if (nextUrl && primaryTab.url !== nextUrl) {
      chrome.tabs.update(primaryTab.id, { url: nextUrl, active: true });
    } else {
      chrome.tabs.update(primaryTab.id, { active: true });
    }
    if (extraTabs.length) {
      chrome.tabs.remove(extraTabs);
    }
  });
}

async function postJson(endpoint, payload) {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    throw new Error(`${endpoint} returned ${response.status}`);
  }
  return response.json();
}

async function postQueuePayload(payload) {
  let lastError = "unknown error";
  for (const endpoint of queueEndpoints) {
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!response.ok) {
        lastError = `${endpoint} returned ${response.status}`;
        continue;
      }
      return response.json();
    } catch (error) {
      lastError = String(error);
    }
  }
  throw new Error(lastError);
}

async function autoQueueStream(tabId, streamUrl) {
  const intentState = autoIntentByTab.get(tabId);
  if (!intentState || intentState.queued || !streamUrl) {
    return;
  }
  intentState.queued = true;
  autoIntentByTab.set(tabId, intentState);
  const pageState = pageStateByTab.get(tabId) || {};
  const pageMetadata = pageState.metadata || {};
  const intent = intentState.intent || {};
  const metadata = {
    ...pageMetadata,
    raw_title: intent.title || pageMetadata.raw_title || pageState.page_title || "Detected stream",
    series_name: intent.title || pageMetadata.series_name || pageMetadata.raw_title || "",
    year: intent.year || pageMetadata.year || null,
    season: intent.season || pageMetadata.season || null,
    episode: intent.episode || pageMetadata.episode || null,
    media_type: intent.media_type || pageMetadata.media_type || "movie",
    poster_url: intent.poster_url || pageMetadata.poster_url || "",
    backdrop_url: intent.backdrop_url || pageMetadata.backdrop_url || "",
    page_url: pageState.page_url || ""
  };
  try {
    await postQueuePayload({
      title: normalizeTitle(metadata),
      url: streamUrl,
      metadata
    });
    if (intentState.closeWhenQueued) {
      setTimeout(() => {
        chrome.tabs.remove(tabId, () => {
          if (chrome.runtime.lastError) {
            console.warn("auto tab close warning", chrome.runtime.lastError.message);
          }
        });
      }, 1200);
    }
  } catch (error) {
    intentState.queued = false;
    autoIntentByTab.set(tabId, intentState);
    throw error;
  }
}

async function publishState(tabId) {
  const state = pageStateByTab.get(tabId) || {};
  const historyState = historyByTab.get(tabId) || { entries: [], index: -1 };
  const payload = {
    page_url: state.page_url || "",
    page_title: state.page_title || "",
    metadata: state.metadata || {},
    streams: streamsByTab.get(tabId) || [],
    can_go_back: historyState.index > 0,
    can_go_forward: historyState.index >= 0 && historyState.index < historyState.entries.length - 1
  };
  let lastError = "unknown error";
  for (const endpoint of stateEndpoints()) {
    try {
      await postJson(endpoint, payload);
      return;
    } catch (error) {
      lastError = String(error);
    }
  }
  throw new Error(lastError);
}

async function fetchJson(endpoint) {
  const response = await fetch(endpoint);
  if (!response.ok) {
    throw new Error(`${endpoint} returned ${response.status}`);
  }
  return response.json();
}

async function pollBrowserCommand() {
  let command = null;
  for (const endpoint of commandEndpoints()) {
    try {
      command = await fetchJson(endpoint);
      break;
    } catch (_error) {
      command = null;
    }
  }
  if (!command || !command.action || !command.id || command.id <= lastCommandId) {
    return;
  }
  const handled = command.action === "navigate" && command.value
    ? await executeBrowserNavigation(command.value)
    : await executeBrowserCommand(command.action);
  if (!handled) {
    return;
  }
  lastCommandId = command.id;
  for (const endpoint of commandEndpoints()) {
    try {
      await postJson(`${endpoint}/ack`, { command_id: command.id });
      break;
    } catch (_error) {
      // try next endpoint
    }
  }
}

function getPrimaryTab(callback) {
  chrome.tabs.query({}, (tabs) => {
    const lastStateTab = lastStateTabId == null ? null : tabs.find((tab) => tab.id === lastStateTabId);
    const preferred = Array.from(primaryTabByWindow.values())
      .map((tabId) => tabs.find((tab) => tab.id === tabId))
      .find(Boolean);
    const fallback = tabs.find((tab) => isSupportedUrl(tab.url)) || tabs[0] || null;
    callback(lastStateTab || preferred || fallback || null);
  });
}

function executeBrowserNavigation(url) {
  return new Promise((resolve) => {
    getPrimaryTab((tab) => {
      if (!tab || tab.id == null) {
        resolve(false);
        return;
      }
      if (hasAutoIntentParams(url)) {
        const prepareAutoTab = (autoTabId, closeWhenQueued) => {
          if (autoTabId != null) {
            const intent = autoIntentFromUrl(url);
            if (intent) {
              autoIntentByTab.set(autoTabId, { intent, queued: false, closeWhenQueued: !!closeWhenQueued });
            }
            lastStateTabId = autoTabId;
            streamsByTab.set(autoTabId, []);
            historyByTab.set(autoTabId, { entries: [url], index: 0 });
            pageStateByTab.set(autoTabId, {
              page_url: url,
              page_title: "",
              metadata: {}
            });
            publishState(autoTabId).catch(() => {});
          }
        };
        const navigateCurrentTab = () => {
          chrome.tabs.update(tab.id, { url, active: true }, () => {
            if (chrome.runtime.lastError) {
              console.warn("auto navigation update warning", chrome.runtime.lastError.message);
              resolve(false);
              return;
            }
            prepareAutoTab(tab.id, false);
            resolve(true);
          });
        };
        chrome.tabs.create({ windowId: tab.windowId, url, active: false }, (createdTab) => {
          if (chrome.runtime.lastError) {
            console.warn("auto navigation create warning", chrome.runtime.lastError.message);
            navigateCurrentTab();
            return;
          }
          prepareAutoTab(createdTab?.id, true);
          resolve(true);
        });
        return;
      }
      if (tab.windowId != null) {
        updatePrimaryTab(tab.windowId, tab.id);
      }
      lastStateTabId = tab.id;
      streamsByTab.set(tab.id, []);
      if (!hasAutoIntentParams(url)) {
        autoIntentByTab.delete(tab.id);
      }
      historyByTab.set(tab.id, { entries: [url], index: 0 });
      pageStateByTab.set(tab.id, {
        page_url: url,
        page_title: "",
        metadata: {}
      });
      publishState(tab.id).catch(() => {});
      chrome.tabs.sendMessage(tab.id, { type: "navigate", url }, (response) => {
        if (chrome.runtime.lastError) {
          chrome.tabs.update(tab.id, { url, active: true }, () => {
            if (chrome.runtime.lastError) {
              console.warn("browser navigate update warning", chrome.runtime.lastError.message);
            }
            resolve(true);
          });
          return;
        }
        if (response && response.ok) {
          resolve(true);
          return;
        }
        chrome.tabs.update(tab.id, { url, active: true }, () => {
          if (chrome.runtime.lastError) {
            console.warn("browser navigate update warning", chrome.runtime.lastError.message);
          }
          resolve(true);
        });
      });
    });
  });
}

function executeBrowserCommand(action) {
  return new Promise((resolve) => {
    getPrimaryTab((tab) => {
      if (!tab || tab.id == null) {
        resolve(false);
        return;
      }
      if (action === "back") {
        const historyState = historyByTab.get(tab.id) || { entries: [], index: -1 };
        if (historyState.index <= 0) {
          resolve(false);
          return;
        }
        historyState.index -= 1;
        historyByTab.set(tab.id, historyState);
        chrome.tabs.goBack(tab.id, () => {
          publishState(tab.id).catch(() => {});
          resolve(true);
        });
        return;
      }
      if (action === "forward") {
        const historyState = historyByTab.get(tab.id) || { entries: [], index: -1 };
        if (historyState.index < 0 || historyState.index >= historyState.entries.length - 1) {
          resolve(false);
          return;
        }
        historyState.index += 1;
        historyByTab.set(tab.id, historyState);
        chrome.tabs.goForward(tab.id, () => {
          publishState(tab.id).catch(() => {});
          resolve(true);
        });
        return;
      }
      if (action === "reload") {
        chrome.tabs.reload(tab.id, {}, () => resolve(true));
        return;
      }
      resolve(false);
    });
  });
}

chrome.webRequest.onBeforeRequest.addListener(
  (details) => rememberStream(details.tabId, details),
  { urls: ["<all_urls>"] }
);

chrome.tabs.onRemoved.addListener((tabId) => {
  streamsByTab.delete(tabId);
  pageStateByTab.delete(tabId);
  autoIntentByTab.delete(tabId);
  historyByTab.delete(tabId);
  for (const [windowId, primaryTabId] of primaryTabByWindow.entries()) {
    if (primaryTabId === tabId) {
      primaryTabByWindow.delete(windowId);
    }
  }
});

chrome.tabs.onCreated.addListener((tab) => {
  if (tab.windowId == null) {
    return;
  }
  if (isAutoIntentTab(tab)) {
    return;
  }
  keepSingleBrowserTab(tab.windowId, primaryTabByWindow.get(tab.windowId), null);
});

chrome.windows.onCreated.addListener((window) => {
  if (window.id == null) {
    return;
  }
  chrome.tabs.query({ windowId: window.id }, (tabs) => {
    const primaryTabId = Array.from(primaryTabByWindow.values())[0] || null;
    if (primaryTabId != null) {
      chrome.windows.remove(window.id);
      chrome.tabs.update(primaryTabId, { active: true });
      return;
    }
    const firstTab = tabs[0];
    if (firstTab?.id != null) {
      primaryTabByWindow.set(window.id, firstTab.id);
    }
  });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (tab.windowId != null) {
    updatePrimaryTab(tab.windowId, tabId);
  }
  if (isSupportedUrl(changeInfo.url || tab.url || "")) {
    lastStateTabId = tabId;
  }
  if (changeInfo.url) {
    const keepsAutoIntent =
      autoIntentByTab.has(tabId) &&
      isSupportedUrl(changeInfo.url) &&
      /\/watch\//.test(changeInfo.url);
    if (!hasAutoIntentParams(changeInfo.url) && !keepsAutoIntent) {
      autoIntentByTab.delete(tabId);
    }
    if (isSupportedUrl(changeInfo.url)) {
      keepSingleBrowserTab(tab.windowId, primaryTabByWindow.get(tab.windowId) || tabId, changeInfo.url);
      return;
    }
    if (tab.windowId != null) {
      keepSingleBrowserTab(tab.windowId, primaryTabByWindow.get(tab.windowId) || tabId, DEFAULT_BROWSER_URL);
    }
    return;
  }
  if (changeInfo.status === "loading" && tab.url && isSupportedUrl(tab.url) && /\/watch\//.test(tab.url)) {
    if (tab.windowId != null) {
      updatePrimaryTab(tab.windowId, tabId);
    }
    streamsByTab.set(tabId, []);
    historyByTab.set(tabId, { entries: [tab.url], index: 0 });
    pageStateByTab.set(tabId, {
      page_url: tab.url,
      page_title: tab.title || "",
      metadata: {}
    });
    publishState(tabId).catch(() => {});
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const tabId = sender.tab ? sender.tab.id : null;

  if (message.type === "getStreams") {
    sendResponse({ streams: tabId == null ? [] : (streamsByTab.get(tabId) || []) });
    return true;
  }

  if (message.type === "pageState" && tabId != null) {
    lastStateTabId = tabId;
    const previousState = pageStateByTab.get(tabId) || {};
    const nextPageUrl = message.page_url || "";
    const previousMetadata = previousState.metadata || {};
    const nextMetadata = message.metadata || {};
    const previousBase = String(previousState.page_url || "").split("#")[0];
    const nextBase = String(nextPageUrl).split("#")[0];
    const previousEpisode = String(previousState.page_url || "").match(/(?:#|&)ep=(\d+,\d+)/i)?.[1] || "";
    const nextEpisode = String(nextPageUrl).match(/(?:#|&)ep=(\d+,\d+)/i)?.[1] || "";
    const previousSeasonNumber = String(previousMetadata.season || "");
    const nextSeasonNumber = String(nextMetadata.season || "");
    const previousEpisodeNumber = String(previousMetadata.episode || "");
    const nextEpisodeNumber = String(nextMetadata.episode || "");
    const metadataEpisodeChanged =
      previousSeasonNumber !== nextSeasonNumber || previousEpisodeNumber !== nextEpisodeNumber;
    if (previousBase !== nextBase || previousEpisode !== nextEpisode || metadataEpisodeChanged) {
      streamsByTab.set(tabId, []);
    }
    const historyState = historyByTab.get(tabId) || { entries: [], index: -1 };
    if (nextPageUrl) {
      if (historyState.index >= 0 && historyState.entries[historyState.index] !== nextPageUrl) {
        const existingIndex = historyState.entries.indexOf(nextPageUrl);
        if (existingIndex >= 0 && Math.abs(existingIndex - historyState.index) === 1) {
          historyState.index = existingIndex;
        } else {
          historyState.entries = historyState.entries.slice(0, historyState.index + 1);
          historyState.entries.push(nextPageUrl);
          historyState.index = historyState.entries.length - 1;
        }
      } else if (historyState.index < 0) {
        historyState.entries = [nextPageUrl];
        historyState.index = 0;
      }
      historyByTab.set(tabId, historyState);
    }
    pageStateByTab.set(tabId, {
      page_url: nextPageUrl,
      page_title: message.page_title || "",
      metadata: nextMetadata
    });
    publishState(tabId)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message.type === "autoIntent" && tabId != null) {
    const existing = autoIntentByTab.get(tabId) || {};
    autoIntentByTab.set(tabId, {
      intent: message.intent || {},
      queued: false,
      closeWhenQueued: !!existing.closeWhenQueued
    });
    sendResponse({ ok: true });
    return true;
  }

  if (message.type === "queueStream") {
    const streamUrl = message.url;
    const metadata = message.metadata || {};
    const payload = {
      title: normalizeTitle(metadata),
      url: streamUrl,
      metadata
    };
    postQueuePayload(payload)
      .then((task) => sendResponse({ ok: true, task }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  return false;
});

setInterval(() => {
  pollBrowserCommand().catch(() => {});
}, 1000);
