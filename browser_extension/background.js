const streamsByTab = new Map();
const pageStateByTab = new Map();
const primaryTabByWindow = new Map();
const historyByTab = new Map();
let lastCommandId = 0;
const queueEndpoints = [
  "http://app:8765/api/browser/queue",
  "http://downloader-app:8765/api/browser/queue",
  "http://127.0.0.1:8765/api/browser/queue",
  "http://localhost:8765/api/browser/queue"
];

function normalizeTitle(metadata) {
  const rawTitle = (metadata.raw_title || "Untitled").trim().replace(/\s*[-|]\s*Y?Flix.*$/i, "").trim();
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
  }
  streamsByTab.set(tabId, existing.slice(0, 20));
  publishState(tabId).catch(() => {});
}

function isYflixUrl(url) {
  return typeof url === "string" && url.startsWith("https://yflix.to/");
}

function stateEndpoints() {
  return [
    "http://app:8765/api/browser/state",
    "http://downloader-app:8765/api/browser/state",
    "http://127.0.0.1:8765/api/browser/state",
    "http://localhost:8765/api/browser/state"
  ];
}

function commandEndpoints() {
  return [
    "http://app:8765/api/browser/command",
    "http://downloader-app:8765/api/browser/command",
    "http://127.0.0.1:8765/api/browser/command",
    "http://localhost:8765/api/browser/command"
  ];
}

function pickPrimaryTab(tabs) {
  return tabs.find((tab) => isYflixUrl(tab.url)) || tabs[0] || null;
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
    const extraTabs = tabs.filter((tab) => tab.id !== primaryTab.id).map((tab) => tab.id).filter((tabId) => tabId != null);
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
  const handled = await executeBrowserCommand(command.action);
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
    const preferred = Array.from(primaryTabByWindow.values())
      .map((tabId) => tabs.find((tab) => tab.id === tabId))
      .find(Boolean);
    const fallback = tabs.find((tab) => isYflixUrl(tab.url)) || tabs[0] || null;
    callback(preferred || fallback || null);
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
  if (changeInfo.url) {
    if (isYflixUrl(changeInfo.url)) {
      keepSingleBrowserTab(tab.windowId, primaryTabByWindow.get(tab.windowId) || tabId, changeInfo.url);
      return;
    }
    if (tab.windowId != null) {
      keepSingleBrowserTab(tab.windowId, primaryTabByWindow.get(tab.windowId) || tabId, "https://yflix.to/");
    }
    return;
  }
  if (changeInfo.status === "loading" && tab.url && tab.url.startsWith("https://yflix.to/watch/")) {
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
    const previousState = pageStateByTab.get(tabId) || {};
    const nextPageUrl = message.page_url || "";
    const previousBase = String(previousState.page_url || "").split("#")[0];
    const nextBase = String(nextPageUrl).split("#")[0];
    const previousEpisode = String(previousState.page_url || "").match(/(?:#|&)ep=(\d+,\d+)/i)?.[1] || "";
    const nextEpisode = String(nextPageUrl).match(/(?:#|&)ep=(\d+,\d+)/i)?.[1] || "";
    if (previousBase !== nextBase || previousEpisode !== nextEpisode) {
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
      metadata: message.metadata || {}
    });
    publishState(tabId)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message.type === "queueStream") {
    const streamUrl = message.url;
    const metadata = message.metadata || {};
    const payload = JSON.stringify({
      title: normalizeTitle(metadata),
      url: streamUrl,
      metadata
    });
    (async () => {
      let lastError = "unknown error";
      for (const endpoint of queueEndpoints) {
        try {
          const response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: payload
          });
          if (!response.ok) {
            lastError = `${endpoint} returned ${response.status}`;
            continue;
          }
          const task = await response.json();
          sendResponse({ ok: true, task });
          return;
        } catch (error) {
          lastError = String(error);
        }
      }
      sendResponse({ ok: false, error: lastError });
    })();
    return true;
  }

  return false;
});

setInterval(() => {
  pollBrowserCommand().catch(() => {});
}, 1000);
