(function () {
  if (window.__isambardDownloaderInstalled) {
    return;
  }
  window.__isambardDownloaderInstalled = true;
  const SUPPORTED_HOSTS = new Set(["yflix.to", "dashflix.top"]);
  const HOST = location.hostname;
  let yflixAutoStarted = false;
  let yflixAutoAttempts = 0;

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
    const hashMatch = location.hash.match(/(?:#|&)ep=(\d+)(?:,(\d+))?/i);
    if (hashMatch) {
      return { season: hashMatch[1], episode: hashMatch[2] || null };
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
    if (HOST === "yflix.to") {
      return extractYflixTitle();
    }
    if (HOST === "dashflix.top") {
      return extractDashflixTitle();
    }
    return "";
  }

  function extractYflixTitle() {
    const title = normalizeVisibleText(document.querySelector("#filmDetail h1.title, h1[itemprop='name']")?.textContent || "");
    if (title) {
      return title;
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

  function yflixAutoIntentFromUrl() {
    if (HOST !== "yflix.to") {
      return null;
    }
    const queryParams = new URLSearchParams(location.search);
    const hashParams = new URLSearchParams(location.hash.replace(/^#/, ""));
    const params = hashParams.has("isambard_title") ? hashParams : queryParams;
    const title = normalizeVisibleText(params.get("isambard_title") || "");
    const keyword = normalizeVisibleText(queryParams.get("keyword") || params.get("keyword") || "");
    if (!title && !keyword) {
      return null;
    }
    return {
      title: title || keyword,
      year: normalizeVisibleText(params.get("isambard_year") || ""),
      media_type: normalizeVisibleText(params.get("isambard_media_type") || "movie").toLowerCase(),
      season: normalizeVisibleText(params.get("isambard_season") || ""),
      episode: normalizeVisibleText(params.get("isambard_episode") || ""),
      poster_url: params.get("isambard_poster_url") || "",
      backdrop_url: params.get("isambard_backdrop_url") || ""
    };
  }

  function normalizeMatchText(value) {
    return normalizeVisibleText(value)
      .toLowerCase()
      .replace(/&/g, "and")
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  function yflixResultCandidates() {
    return Array.from(document.querySelectorAll(".film-section .item, .item"))
      .map((item) => {
        const titleLink = item.querySelector(".info a.title, a.title");
        const posterLink = item.querySelector("a.poster[href*='/watch/']");
        const href = titleLink?.href || posterLink?.href || "";
        const metadata = Array.from(item.querySelectorAll(".metadata span")).map((node) => normalizeVisibleText(node.textContent || ""));
        return {
          href,
          title: normalizeVisibleText(titleLink?.textContent || ""),
          media_type: metadata.some((text) => /^tv$/i.test(text)) ? "tv" : "movie",
          metadata
        };
      })
      .filter((item) => item.href && item.title);
  }

  function scoreYflixCandidate(candidate, intent) {
    const wantedTitle = normalizeMatchText(intent.title);
    const candidateTitle = normalizeMatchText(candidate.title);
    let score = 0;
    if (candidateTitle === wantedTitle) {
      score += 100;
    } else if (candidateTitle.includes(wantedTitle) || wantedTitle.includes(candidateTitle)) {
      score += 45;
    }
    if (intent.media_type && candidate.media_type === intent.media_type) {
      score += 30;
    }
    if (intent.year && candidate.metadata.includes(intent.year)) {
      score += 20;
    }
    return score;
  }

  function buildYflixWatchUrl(href, intent) {
    const url = new URL(href, location.href);
    if (intent.media_type === "tv") {
      url.hash = `ep=${intent.season || "1"},${intent.episode || "1"}`;
    } else {
      url.hash = "ep=1";
    }
    url.searchParams.set("isambard_title", intent.title || "");
    url.searchParams.set("isambard_media_type", intent.media_type || "movie");
    if (intent.year) {
      url.searchParams.set("isambard_year", intent.year);
    }
    if (intent.season) {
      url.searchParams.set("isambard_season", intent.season);
    }
    if (intent.episode) {
      url.searchParams.set("isambard_episode", intent.episode);
    }
    if (intent.poster_url) {
      url.searchParams.set("isambard_poster_url", intent.poster_url);
    }
    if (intent.backdrop_url) {
      url.searchParams.set("isambard_backdrop_url", intent.backdrop_url);
    }
    return url.toString();
  }

  function notifyAutoIntent(intent) {
    chrome.runtime.sendMessage({ type: "autoIntent", intent }, () => void chrome.runtime.lastError);
  }

  function runYflixSearchAutoFind(intent) {
    const candidates = yflixResultCandidates()
      .map((candidate) => ({ ...candidate, score: scoreYflixCandidate(candidate, intent) }))
      .sort((a, b) => b.score - a.score);
    const best = candidates[0];
    if (!best || best.score < 30) {
      return false;
    }
    notifyAutoIntent(intent);
    location.href = buildYflixWatchUrl(best.href, intent);
    return true;
  }

  function clickYflixPlayback() {
    const selectors = [".goto-play", "#player .player-btn", ".player-main.playable", "video"];
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      if (!node) {
        continue;
      }
      node.scrollIntoView?.({ block: "center" });
      node.click?.();
    }
  }

  function runYflixWatchAutoFind(intent) {
    notifyAutoIntent(intent);
    const desiredEpisode = intent.media_type === "tv"
      ? `ep=${intent.season || "1"},${intent.episode || "1"}`
      : "ep=1";
    if (!location.hash.includes(desiredEpisode)) {
      const episodeLink = Array.from(document.querySelectorAll("#filmEps a[href*='#ep=']"))
        .find((anchor) => String(anchor.href || "").includes(`#${desiredEpisode}`));
      if (episodeLink?.href) {
        location.href = buildYflixWatchUrl(episodeLink.href, intent);
        return true;
      }
      location.hash = desiredEpisode;
    }
    setTimeout(clickYflixPlayback, 400);
    setTimeout(clickYflixPlayback, 1600);
    setTimeout(clickYflixPlayback, 3600);
    return true;
  }

  function runYflixAutoFind() {
    const intent = yflixAutoIntentFromUrl();
    if (!intent || yflixAutoStarted || yflixAutoAttempts > 12) {
      return;
    }
    yflixAutoStarted = true;
    yflixAutoAttempts += 1;
    setTimeout(() => {
      let handled = false;
      if (location.pathname.startsWith("/browser") || location.pathname.startsWith("/filter")) {
        handled = runYflixSearchAutoFind(intent);
      } else if (location.pathname.startsWith("/watch/")) {
        handled = runYflixWatchAutoFind(intent);
      }
      if (!handled && (location.pathname.startsWith("/browser") || location.pathname.startsWith("/filter"))) {
        yflixAutoStarted = false;
      }
    }, 700);
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
    runYflixAutoFind();
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
