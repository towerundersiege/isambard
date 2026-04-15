(function () {
  if (window.__isambardDownloaderInstalled) {
    return;
  }
  window.__isambardDownloaderInstalled = true;
  const SUPPORTED_HOSTS = new Set(["yflix.to", "dashflix.top"]);
  const HOST = location.hostname;

  function parseSeasonEpisode() {
    if (HOST === "yflix.to") {
      return parseSeasonEpisodeForYflix();
    }
    if (HOST === "dashflix.top") {
      return parseSeasonEpisodeForDashflix();
    }
    return parseSeasonEpisodeFallback();
  }

  function parseSeasonEpisodeForYflix() {
    const hashMatch = location.hash.match(/(?:#|&)ep=(\d+),(\d+)/i);
    if (hashMatch) {
      return { season: hashMatch[1], episode: hashMatch[2] };
    }
    return parseSeasonEpisodeFallback();
  }

  function parseSeasonEpisodeForDashflix() {
    const controlEpisode = parseSeasonEpisodeFromControls();
    if (controlEpisode.season || controlEpisode.episode) {
      return controlEpisode;
    }
    return parseSeasonEpisodeFallback();
  }

  function parseSeasonEpisodeFallback() {
    const text = [document.title, document.body.innerText || ""].join(" ");
    const compactMatch = text.match(/\bS(\d{1,2})E(\d{1,2})\b/i);
    if (compactMatch) {
      return { season: compactMatch[1], episode: compactMatch[2] };
    }
    const verboseMatch = text.match(/Season\s+(\d+)\D+Episode\s+(\d+)/i);
    if (verboseMatch) {
      return { season: verboseMatch[1], episode: verboseMatch[2] };
    }
    return { season: null, episode: null };
  }

  function selectedTextForControl(node) {
    if (!node) {
      return "";
    }
    if (node instanceof HTMLSelectElement) {
      return (node.selectedOptions[0]?.textContent || "").replace(/\s+/g, " ").trim();
    }
    const selectedChild = node.querySelector("[aria-selected='true'], .selected, .active");
    if (selectedChild) {
      return (selectedChild.textContent || "").replace(/\s+/g, " ").trim();
    }
    return (node.textContent || "").replace(/\s+/g, " ").trim();
  }

  function parseSeasonEpisodeFromControls() {
    const labels = currentControlTexts();
    let season = null;
    let episode = null;
    for (const text of labels) {
      const seasonMatch = text.match(/^season\s+(\d+)\b/i);
      if (seasonMatch) {
        season = seasonMatch[1];
      }
      const episodeMatch = text.match(/^episode\s+(\d+)\b/i) || text.match(/^e(?:pisode)?\s*(\d+)\b/i);
      if (episodeMatch) {
        episode = episodeMatch[1];
      }
    }
    return { season, episode };
  }

  function currentControlTexts() {
    const selectors = [
      "select option:checked",
      "[role='combobox'] [aria-selected='true']",
      "[role='listbox'] [aria-selected='true']",
      "button[aria-expanded] span",
      "button[aria-haspopup='listbox']",
      "button[aria-haspopup='menu']",
      ".selected",
      ".active"
    ];
    const values = new Set();
    for (const selector of selectors) {
      const nodes = Array.from(document.querySelectorAll(selector));
      for (const node of nodes) {
        const text = (node.textContent || "").replace(/\s+/g, " ").trim();
        if (!text) {
          continue;
        }
        if (/^(server|servers?)\b/i.test(text)) {
          continue;
        }
        if (/^(season\s+\d+|episode\s+\d+|episode\s+\d+\s*[:|-].+|e\d+\b)/i.test(text)) {
          values.add(text);
        }
      }
    }
    return Array.from(values);
  }

  function stripEpisodeContext(value) {
    return String(value || "")
      .replace(/\s*[-|]\s*Y?Flix.*$/i, "")
      .replace(/\s*[-|]\s*DashFlix.*$/i, "")
      .replace(/(?:\s*[-:|]?\s*(?:Season\s*\d+|Episode\s*\d+|S\d{1,2}E\d{1,2}|Ep\.?\s*\d+).*)$/i, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function extractMetadata() {
    const pageText = document.body.innerText || "";
    const titleNode = document.querySelector("h1, .title, [data-title]");
    const rawTitle = (extractSiteSpecificTitle() || titleNode?.textContent || document.title || "Untitled").trim();
    const urlEpisode = parseSeasonEpisode();
    const releaseYearText = HOST === "dashflix.top"
      ? (document.querySelector(".release-year")?.textContent || "")
      : "";
    const yearMatch = [releaseYearText, rawTitle, document.title, pageText]
      .map((text) => String(text || "").match(/\b(19|20)\d{2}\b/))
      .find(Boolean);
    const episodeTitle = extractEpisodeTitle(pageText, urlEpisode.episode);
    const seriesName = stripEpisodeContext(rawTitle) || stripEpisodeContext(document.title) || rawTitle;
    return {
      raw_title: rawTitle,
      series_name: seriesName,
      year: yearMatch ? yearMatch[0] : null,
      season: urlEpisode.season,
      episode: urlEpisode.episode,
      episode_title: episodeTitle
    };
  }

  function extractSiteSpecificTitle() {
    if (HOST === "dashflix.top") {
      return extractDashflixTitle();
    }
    return "";
  }

  function extractDashflixTitle() {
    const tvTitle = normalizeDashflixTitle(document.querySelector(".tv-title-info")?.textContent || "");
    const movieTitle = normalizeDashflixTitle(document.querySelector(".movie-title-info")?.textContent || "");
    if (tvTitle) {
      return tvTitle;
    }
    if (movieTitle) {
      return movieTitle;
    }
    return "";
  }

  function normalizeDashflixTitle(value) {
    const text = normalizeVisibleText(value)
      .replace(/\s*-\s*watch now on dashflix/gi, "")
      .trim();
    if (!text) {
      return "";
    }
    if (/season\s+\d+|episode\s+\d+|server/i.test(text)) {
      return "";
    }
    if (text.length > 120) {
      return "";
    }
    return text;
  }

  function normalizeVisibleText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function extractEpisodeTitle(pageText, episodeNumber) {
    if (HOST === "dashflix.top") {
      for (const text of currentControlTexts()) {
        const derived = parseEpisodeTitle(text, episodeNumber);
        if (derived) {
          return derived;
        }
      }
    }
    const selectors = [
      "[aria-current='true']",
      ".active",
      ".selected",
      "[class*='active']",
      "[class*='episode']"
    ];
    for (const selector of selectors) {
      const nodes = Array.from(document.querySelectorAll(selector));
      for (const node of nodes) {
        const text = (node.textContent || "").replace(/\s+/g, " ").trim();
        const derived = parseEpisodeTitle(text, episodeNumber);
        if (derived) {
          return derived;
        }
      }
    }
    return parseEpisodeTitle(pageText, episodeNumber);
  }

  function parseEpisodeTitle(text, episodeNumber) {
    const compact = (text || "").replace(/\s+/g, " ").trim();
    if (!compact) {
      return "";
    }
    if (episodeNumber) {
      const patterns = [
        new RegExp(`Episode\\s*${episodeNumber}\\s*[-:|]\\s*([^\\n]+)`, "i"),
        new RegExp(`E(?:pisode)?\\s*${episodeNumber}\\s*[-:|]\\s*([^\\n]+)`, "i"),
        new RegExp(`\\b${episodeNumber}\\b\\s*[-:|]\\s*([^\\n]+)`, "i")
      ];
      for (const pattern of patterns) {
        const match = compact.match(pattern);
        if (match && match[1]) {
          return match[1].trim();
        }
      }
    }
    return "";
  }

  function installNavigationGuards() {
    if (window.__isambardNavGuardInstalled) {
      return;
    }
    window.__isambardNavGuardInstalled = true;

    const originalOpen = window.open;
    window.open = function(url, target, features) {
      const href = String(url || "");
      if (!href) {
        return null;
      }
      if (isSupportedHref(href)) {
        location.href = href;
      }
      return null;
    };

    document.addEventListener("click", (event) => {
      const anchor = event.target instanceof Element ? event.target.closest("a") : null;
      if (!anchor) {
        return;
      }
      const href = anchor.href || "";
      if (!href || anchor.target !== "_blank") {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (isSupportedHref(href)) {
        location.href = href;
      }
    }, true);
  }

  function isSupportedHref(href) {
    try {
      const parsed = new URL(href, location.href);
      return SUPPORTED_HOSTS.has(parsed.hostname);
    } catch (_error) {
      return false;
    }
  }

  function publishState() {
    chrome.runtime.sendMessage({
      type: "pageState",
      page_url: location.href,
      page_title: document.title,
      metadata: extractMetadata()
    }, () => void chrome.runtime.lastError);
  }

  function updatePulse() {
    if (!SUPPORTED_HOSTS.has(location.hostname)) {
      return;
    }
    publishState();
  }

  function install() {
    installNavigationGuards();
    updatePulse();
    const observer = new MutationObserver(() => {
      if (!SUPPORTED_HOSTS.has(location.hostname)) {
        return;
      }
      updatePulse();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    window.addEventListener("popstate", updatePulse);
    window.addEventListener("hashchange", updatePulse);
    setInterval(updatePulse, 4000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install, { once: true });
  } else {
    install();
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "navigate" || !message.url || !isSupportedHref(message.url)) {
      return false;
    }
    location.href = message.url;
    sendResponse({ ok: true });
    return true;
  });
})();
